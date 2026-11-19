"""New IM SaaS adapter contract: selectors, filter state, read policy.

All DOM fixtures are synthetic; nothing here touches a real account.
"""
import unittest

from playwright.async_api import async_playwright

from core import im_ui


SAAS_SHELL_HTML = """<div id="sub-app">
  <div class="im-saas-viewport-C7YJGz">
    <div id="imSaasContainerId" class="componentsEntrywrapper imContainer imZIndex semi-always-light imLight">
      <div class="conversationFilterTagBarwrapper">
        <div class="conversationFilterTagBartopBar">
          <div class="conversationFilterTagBarcollapseBtn"></div>
          <span class="conversationFilterTagBartitle">分组</span>
        </div>
        <div class="conversationFilterTagBargroupList">
          <div class="conversationFilterTagBargroup">
            <div class="conversationFilterTagBargroupHeader">
              <span class="conversationFilterTagBargroupLabel">消息</span>
            </div>
            <div class="{cls_all}">
              <span class="conversationFilterTagBartagItemLabel">全部</span>
            </div>
            <div class="{cls_fans}">
              <span class="conversationFilterTagBartagItemLabel">粉丝</span>
            </div>
            <div class="{cls_mutual}">
              <span class="conversationFilterTagBartagItemLabel">互关</span>
            </div>
            <div class="{cls_group}">
              <span class="conversationFilterTagBartagItemLabel">群聊</span>
            </div>
          </div>
          <div class="conversationFilterTagBargroup">
            <div class="conversationFilterTagBargroupHeader">
              <span class="conversationFilterTagBargroupLabel">陌生人消息</span>
            </div>
            <div class="{cls_stranger}">
              <span class="conversationFilterTagBartagItemLabel">全部</span>
            </div>
          </div>
        </div>
        <script>
          // Mimic the real store: clicking a tag moves the Active class within
          // its group only (stranger tags stay independent).
          document.querySelectorAll('.conversationFilterTagBargroup').forEach(g => {
            g.querySelectorAll('.conversationFilterTagBartagItem').forEach(tag => {
              tag.addEventListener('click', () => {
                g.querySelectorAll('.conversationFilterTagBartagItem').forEach(t =>
                  t.classList.remove('conversationFilterTagBartagItemActive'));
                tag.classList.add('conversationFilterTagBartagItemActive');
              });
            });
          });
        </script>
      </div>
      <div class="semi-resizable-resizable">
        <div class="conversationConversationListwrapper" id="convList">
          {rows}
        </div>
      </div>
      <div class="RightPanelEmptywrapper"></div>
      <div class="commonAllPopupContainerim-saas-container"></div>
    </div>
  </div>
</div>"""


def saas_row(name, *, extra=""):
    return (
        '<div data-e2e="conversation-item" class="conversationConversationItemwrapper">'
        '<div class="conversationConversationItemrowArea2">'
        '<div class="conversationConversationItemtitleWrapper">'
        f'<div class="conversationConversationItemtitle">{name}</div>'
        '</div>'
        f'<div class="ConversationItemDescwrapper">{extra}</div>'
        '</div></div>'
    )


def saas_page_html(*, active_group=False, active_all=True, active_stranger=False,
                   rows=()):
    # Real bundle emits base class + ``...tagItemActive`` suffix class.
    def cls(active):
        base = "conversationFilterTagBartagItem"
        return base + (" " + base + "Active" if active else "")
    active = {
        "cls_all": cls(active_all),
        "cls_fans": cls(False),
        "cls_mutual": cls(False),
        "cls_group": cls(active_group),
        "cls_stranger": cls(active_stranger),
    }
    body = "".join(saas_row(name) for name in rows)
    html = SAAS_SHELL_HTML
    for key, value in active.items():
        html = html.replace("{" + key + "}", value)
    return html.replace("{rows}", body)


class SaasUiContractTests(unittest.TestCase):
    def test_exported_constants(self):
        self.assertEqual(
            im_ui.SAAS_ROW_SELECTOR, '#sub-app [data-e2e="conversation-item"]'
        )
        # Title node verified in __federation_expose_default_export.aab99327.js:
        # TextKeepSpaces renders <div class="conversationConversationItemtitle">.
        self.assertIn("conversationConversationItemtitle", im_ui.SAAS_NAME_SELECTOR)
        self.assertIsInstance(im_ui.SAAS_SCROLL_SELECTORS, tuple)
        self.assertIn(
            ".conversationConversationListwrapper", im_ui.SAAS_SCROLL_SELECTORS
        )

    def test_filter_tag_labels_match_public_bundle(self):
        # Labels verified in saasApi.getFilterTags(): 消息/全部/粉丝/互关/群聊
        # and 陌生人消息/关注我的.
        labels = im_ui.SAAS_FILTER_TAG_LABELS
        self.assertEqual(labels["friend"], "全部")
        self.assertEqual(labels["group"], "群聊")
        self.assertEqual(labels["stranger"], "陌生人消息")


class SaasFilterStateRealBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def _page(self, html):
        pw = await async_playwright().start()
        self.addAsyncCleanup(pw.stop)
        browser = await pw.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        page = await browser.new_page()
        await page.set_content(html)
        return page

    async def test_is_saas_ui_true_on_synthetic_saas_shell(self):
        page = await self._page(saas_page_html(rows=["甲"]))
        self.assertTrue(await im_ui.is_saas_ui(page))

    async def test_is_saas_ui_false_on_legacy_sub_app(self):
        page = await self._page(
            '<div id="sub-app"><ul><li class="semi-list-item">'
            '<span class="item-header-name-x">甲</span></li></ul></div>'
        )
        self.assertFalse(await im_ui.is_saas_ui(page))

    async def test_saas_filter_is_active_reads_active_tag(self):
        page = await self._page(saas_page_html(active_all=True, active_group=False))
        self.assertTrue(await im_ui.saas_filter_is_active(page, "friend"))
        self.assertFalse(await im_ui.saas_filter_is_active(page, "group"))

    async def test_saas_filter_is_active_group(self):
        page = await self._page(saas_page_html(active_all=False, active_group=True))
        self.assertTrue(await im_ui.saas_filter_is_active(page, "group"))
        self.assertFalse(await im_ui.saas_filter_is_active(page, "friend"))

    async def test_saas_filter_is_active_false_on_legacy_ui(self):
        page = await self._page('<div id="sub-app"><ul></ul></div>')
        self.assertFalse(await im_ui.saas_filter_is_active(page, "friend"))
        self.assertFalse(await im_ui.saas_filter_is_active(page, "group"))

    async def test_saas_filter_is_active_unknown_kind_raises(self):
        page = await self._page(saas_page_html())
        with self.assertRaises(ValueError):
            await im_ui.saas_filter_is_active(page, "stranger-x")

    async def test_saas_filter_is_active_never_clicks(self):
        html = saas_page_html(active_all=True)
        html = html.replace(
            'class="conversationFilterTagBartagItem conversationFilterTagBartagItemActive"',
            'class="conversationFilterTagBartagItem conversationFilterTagBartagItemActive" onclick="window.__clicked=1"',
            1,
        )
        # saas_filter_is_active must not click; the fixture JS only fires on
        # real click events, so window.__clicked stays unset.
        page = await self._page(html)
        await im_ui.saas_filter_is_active(page, "friend")
        self.assertIsNone(await page.evaluate("window.__clicked || null"))

    async def test_activate_saas_filter_friend_and_group(self):
        page = await self._page(saas_page_html(active_all=True))
        # 'friend' maps to the ALL tag of the 消息 group.
        self.assertTrue(await im_ui.activate_saas_filter(page, "friend"))
        self.assertTrue(await im_ui.saas_filter_is_active(page, "friend"))
        self.assertTrue(await im_ui.activate_saas_filter(page, "group"))
        self.assertTrue(await im_ui.saas_filter_is_active(page, "group"))
        self.assertFalse(await im_ui.saas_filter_is_active(page, "friend"))

    async def test_activate_saas_filter_false_on_legacy_ui(self):
        page = await self._page('<div id="sub-app"><ul></ul></div>')
        self.assertFalse(await im_ui.activate_saas_filter(page, "friend"))
        self.assertFalse(await im_ui.activate_saas_filter(page, "group"))

    async def test_activate_saas_filter_raises_when_saas_present_but_unrecognised(self):
        # SaaS shell present but the filter bar is missing/collapsed: the adapter
        # must fail loudly, never silently fall back to the legacy XPath.
        html = saas_page_html().replace("conversationFilterTagBarwrapper", "gone")
        page = await self._page(html)
        with self.assertRaises(RuntimeError):
            await im_ui.activate_saas_filter(page, "friend")

    async def test_saas_row_name_selector(self):
        page = await self._page(saas_page_html(rows=["测试好友"]))
        row = page.locator(im_ui.SAAS_ROW_SELECTOR).first
        name = await row.locator(im_ui.SAAS_NAME_SELECTOR).first.inner_text()
        self.assertEqual("测试好友", name)

    async def test_activate_saas_filter_raises_when_click_never_takes_effect(self):
        # SaaS shell + filter bar present, but the tag click is a no-op (the
        # store never marks it active). Returning False here would let the
        # caller fall back to the legacy XPath on a page that is definitely
        # the new UI — the adapter must raise instead.
        html = saas_page_html(active_all=False).replace(
            "<script>", "<script>window.__noop=1;//"
        )
        # Neutralise every click listener the fixture installs.
        html = html.replace("tag.addEventListener('click'", "void 0 && tag.addEventListener('click'")
        page = await self._page(html)
        with self.assertRaises(RuntimeError):
            await im_ui.activate_saas_filter(page, "friend", timeout_ms=800)

    async def test_is_saas_ui_propagates_read_errors(self):
        # A broken DOM read must not be reported as "legacy UI": that would
        # silently route a SaaS page into the old XPath collectors.
        page = await self._page(saas_page_html(rows=["甲"]))
        await page.close()
        with self.assertRaises(Exception):
            await im_ui.is_saas_ui(page)

    async def test_wait_for_saas_ui_detects_late_micro_frontend(self):
        # The SaaS micro-frontend mounts after the legacy-looking shell; a
        # single instant probe would miss it and send the refresh down the
        # legacy path where it can never succeed.
        html = (
            '<div id="sub-app"><div class="semi-list-item">legacy</div>'
            '<script>setTimeout(() => {'
            'document.body.insertAdjacentHTML("beforeend", '
            '\'<div class="im-saas-viewport-C7YJGz" style="width:100px;height:100px">x</div>\');'
            '}, 300);</script></div>'
        )
        page = await self._page(html)
        self.assertTrue(await im_ui.wait_for_saas_ui(page, timeout_ms=5000))

    async def test_wait_for_saas_ui_returns_false_on_legacy_page(self):
        page = await self._page('<div id="sub-app"><ul><li class="semi-list-item">甲</li></ul></div>')
        self.assertFalse(await im_ui.wait_for_saas_ui(page, timeout_ms=600))

    async def test_wait_for_saas_ui_stops_early_when_legacy_probe_fires(self):
        # When the legacy list is already rendered, the wait must not burn the
        # whole SaaS budget before letting the legacy collector run.
        page = await self._page(
            '<div id="sub-app"><ul><li class="semi-list-item">甲</li></ul></div>'
        )

        async def legacy_probe():
            return await page.locator("#sub-app li.semi-list-item").count() > 0

        import time
        started = time.monotonic()
        self.assertFalse(
            await im_ui.wait_for_saas_ui(
                page, timeout_ms=10000, legacy_probe=legacy_probe
            )
        )
        self.assertLess(time.monotonic() - started, 5)


class SaasReadPolicyTests(unittest.TestCase):
    def test_saas_read_policy_allows_observed_init_reads(self):
        allowed = im_ui.saas_request_allowed
        # Reads observed on the live SaaS page load (passive capture).
        self.assertTrue(allowed("POST", "https://abtestvm.bytedance.com/service/2/abtest_config/"))
        self.assertTrue(allowed("POST", "https://creator.amemv.com/api/v1/workspace/feature_gates/values"))
        self.assertTrue(allowed("POST", "https://www.douyin.com/aweme/v1/web/im/user/info/"))
        self.assertTrue(allowed("POST", "https://www.douyin.com/aweme/v1/web/im/user/active/status/"))
        self.assertTrue(allowed("GET", "https://www.douyin.com/aweme/v1/web/im/user/info/"))

    def test_saas_read_policy_still_denies_writes_and_unknowns(self):
        allowed = im_ui.saas_request_allowed
        self.assertFalse(allowed("POST", "https://www.douyin.com/aweme/v1/web/im/user/info/send"))
        self.assertFalse(allowed("POST", "https://imapi.douyin.com/v1/message/send"))
        self.assertFalse(allowed("POST", "https://evil.example.com/service/2/abtest_config/"))
        self.assertFalse(allowed("DELETE", "https://www.douyin.com/aweme/v1/web/im/user/info/"))
        # Third-party stats not observed for reads stay denied.
        self.assertFalse(allowed("POST", "https://mon.zijieapi.com/monitor/collect"))

    def test_saas_request_allowed_signature(self):
        import inspect
        self.assertTrue(callable(im_ui.saas_request_allowed))
        sig = inspect.signature(im_ui.saas_request_allowed)
        self.assertEqual(["method", "url"], list(sig.parameters))


if __name__ == "__main__":
    unittest.main()
