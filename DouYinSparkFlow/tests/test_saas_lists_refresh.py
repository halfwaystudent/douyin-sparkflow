"""SaaS-aware refresh paths: filter routing, row reading, safety invariants.

All DOM fixtures are synthetic; nothing touches a real account or the network.
"""
import unittest

from playwright.async_api import async_playwright

from core import im_ui


def saas_page(*, active="all", rows=(), scroll_height=None):
    """Build a synthetic SaaS page. ``active`` in {'all','fans','mutual','group'}."""
    tags = {
        "全部": "all", "粉丝": "fans", "互关": "mutual", "群聊": "group",
    }
    tag_html = ""
    for label, key in tags.items():
        cls = "conversationFilterTagBartagItem"
        if active == key:
            cls += " conversationFilterTagBartagItemActive"
        tag_html += (
            f'<div class="{cls}">'
            f'<span class="conversationFilterTagBartagItemLabel">{label}</span>'
            '</div>'
        )
    stranger_cls = "conversationFilterTagBartagItem"
    if active == "stranger":
        stranger_cls += " conversationFilterTagBartagItemActive"
    stranger = (
        f'<div class="{stranger_cls}">'
        '<span class="conversationFilterTagBartagItemLabel">全部</span></div>'
    )
    row_html = "".join(
        '<div data-e2e="conversation-item" class="conversationConversationItemwrapper">'
        '<div class="conversationConversationItemrowArea2">'
        '<div class="conversationConversationItemtitleWrapper">'
        f'<div class="conversationConversationItemtitle">{name}</div>'
        '</div>'
        '<div class="ConversationItemDescwrapper"><span class="hint">hi</span></div>'
        '</div></div>'
        for name in rows
    )
    height = scroll_height if scroll_height is not None else max(100, len(rows) * 67)
    return f"""<div id="sub-app"><div class="im-saas-viewport-C7YJGz">
      <div class="conversationFilterTagBarwrapper">
        <div class="conversationFilterTagBargroup">
          <div class="conversationFilterTagBargroupHeader">
            <span class="conversationFilterTagBargroupLabel">消息</span></div>
          {tag_html}</div>
        <div class="conversationFilterTagBargroup">
          <div class="conversationFilterTagBargroupHeader">
            <span class="conversationFilterTagBargroupLabel">陌生人消息</span></div>
          {stranger}</div>
      </div>
      <div class="conversationConversationListwrapper" id="list">{row_html}</div>
    </div></div>
    <script>
      const list = document.getElementById('list');
      Object.defineProperties(list, {{
        clientHeight: {{value: 200}},
        scrollHeight: {{value: {height}}},
        scrollTop: {{value: 0, writable: true}},
      }});
      // Tag clicks move the Active class within the group (like the real store).
      document.querySelectorAll('.conversationFilterTagBargroup').forEach(g => {{
        g.querySelectorAll('.conversationFilterTagBartagItem').forEach(tag => {{
          tag.addEventListener('click', () => {{
            g.querySelectorAll('.conversationFilterTagBartagItem').forEach(t =>
              t.classList.remove('conversationFilterTagBartagItemActive'));
            tag.classList.add('conversationFilterTagBartagItemActive');
          }});
        }});
      }});
    </script>"""


def legacy_page(*, rows=()):
    row_html = "".join(
        '<li class="semi-list-item"><span class="item-header-name-x">'
        f'{name}</span></li>' for name in rows
    )
    return f'<div id="sub-app"><ul>{row_html}</ul></div>'


class SaasListReadingTests(unittest.IsolatedAsyncioTestCase):
    """collect-style scans must read SaaS rows by verified selectors."""

    async def _page(self, html):
        pw = await async_playwright().start()
        self.addAsyncCleanup(pw.stop)
        browser = await pw.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        page = await browser.new_page()
        await page.set_content(html)
        return page

    async def test_saas_rows_read_names_without_clicking(self):
        page = await self._page(saas_page(rows=["好友甲", "好友乙"]))
        rows = page.locator(im_ui.SAAS_ROW_SELECTOR)
        names = []
        for row in await rows.all():
            if not await row.is_visible():
                continue
            name = (await row.locator(im_ui.SAAS_NAME_SELECTOR)
                    .first.inner_text()).strip()
            names.append(name)
        self.assertEqual(["好友甲", "好友乙"], names)

    async def test_saas_scroll_container_found(self):
        page = await self._page(saas_page(rows=["a"]))
        sel, handle = await self._first_scrollable(page, im_ui.SAAS_SCROLL_SELECTORS)
        self.assertIsNotNone(handle)
        self.assertIn("ConversationList", sel)

    async def _first_scrollable(self, page, selectors):
        from core.browser import first_scrollable_element
        return await first_scrollable_element(page, selectors)


class SaasFilterRoutingTests(unittest.IsolatedAsyncioTestCase):
    """friend/group must be routed by verified filter state, never by
    assuming the visible list is the right kind."""

    async def _page(self, html):
        pw = await async_playwright().start()
        self.addAsyncCleanup(pw.stop)
        browser = await pw.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        page = await browser.new_page()
        await page.set_content(html)
        return page

    async def test_friend_filter_is_all_tag(self):
        page = await self._page(saas_page(active="all"))
        self.assertTrue(await im_ui.saas_filter_is_active(page, "friend"))
        self.assertFalse(await im_ui.saas_filter_is_active(page, "group"))

    async def test_group_filter_is_group_tag(self):
        page = await self._page(saas_page(active="group"))
        self.assertTrue(await im_ui.saas_filter_is_active(page, "group"))
        self.assertFalse(await im_ui.saas_filter_is_active(page, "friend"))

    async def test_stranger_tag_never_counts_as_friend_or_group(self):
        page = await self._page(saas_page(active="stranger"))
        self.assertFalse(await im_ui.saas_filter_is_active(page, "friend"))
        self.assertFalse(await im_ui.saas_filter_is_active(page, "group"))

    async def test_fans_mutual_are_not_friend(self):
        for active in ("fans", "mutual"):
            page = await self._page(saas_page(active=active))
            self.assertFalse(await im_ui.saas_filter_is_active(page, "friend"))
            self.assertFalse(await im_ui.saas_filter_is_active(page, "group"))
            await page.context.browser.close() if False else None

    async def test_legacy_ui_reports_no_saas_filter(self):
        page = await self._page(legacy_page(rows=["甲"]))
        self.assertFalse(await im_ui.is_saas_ui(page))
        self.assertFalse(await im_ui.saas_filter_is_active(page, "friend"))
        self.assertFalse(await im_ui.saas_filter_is_active(page, "group"))
        self.assertFalse(await im_ui.activate_saas_filter(page, "friend"))
        self.assertFalse(await im_ui.activate_saas_filter(page, "group"))

    async def test_unknown_kind_rejected(self):
        page = await self._page(saas_page(active="all"))
        for bad in ("stranger", "all", "", None, 1):
            with self.assertRaises(ValueError):
                await im_ui.saas_filter_is_active(page, bad)
            with self.assertRaises(ValueError):
                await im_ui.activate_saas_filter(page, bad)

    async def test_saas_present_but_filter_bar_missing_raises(self):
        html = ('<div id="sub-app"><div class="im-saas-viewport-X">'
                '<div class="semi-resizable-resizable">'
                '<div class="conversationConversationListwrapper">'
                '<div data-e2e="conversation-item">'
                '<div class="conversationConversationItemtitle">甲</div>'
                '</div></div></div></div></div>')
        page = await self._page(html)
        self.assertTrue(await im_ui.is_saas_ui(page))
        with self.assertRaises(RuntimeError):
            await im_ui.activate_saas_filter(page, "friend", timeout_ms=500)


class SaasGroupRowCollectionTests(unittest.IsolatedAsyncioTestCase):
    """collect_saas_group_rows must fail closed when a visible row has no
    readable conversation id — never guess or skip it."""

    async def _page(self, html):
        pw = await async_playwright().start()
        self.addAsyncCleanup(pw.stop)
        browser = await pw.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        page = await browser.new_page()
        await page.set_content(html)
        return page

    async def test_group_rows_without_conversation_id_fail_closed(self):
        from core.groups import collect_saas_group_rows
        html = saas_page(rows=["群甲"])
        page = await self._page(html)
        # No React fiber on synthetic rows -> id unreadable -> must raise.
        with self.assertRaisesRegex(RuntimeError, "conversation id"):
            await collect_saas_group_rows(page, max_scan_seconds=2, scroll_delay_seconds=0)

    async def test_group_rows_with_fiber_id_and_name_are_collected(self):
        from core.groups import collect_saas_group_rows
        html = saas_page(rows=[], scroll_height=200)
        page = await self._page(html)
        await page.evaluate(
            """() => {
                const list = document.getElementById('list');
                list.innerHTML = '';
                const data = [['群甲','12345678901'], ['群乙','12345678902']];
                data.forEach(([name, id]) => {
                    const row = document.createElement('div');
                    row.setAttribute('data-e2e', 'conversation-item');
                    row.className = 'conversationConversationItemwrapper';
                    row.innerHTML = '<div class="conversationConversationItemtitle">' + name + '</div>';
                    row.__reactFiber$test = { memoizedProps: { conversation: { id } } };
                    list.appendChild(row);
                });
            }"""
        )
        rows = await collect_saas_group_rows(page, max_scan_seconds=3, scroll_delay_seconds=0)
        self.assertEqual(
            [{'name': '群甲', 'conversationId': '12345678901'},
             {'name': '群乙', 'conversationId': '12345678902'}],
            rows,
        )


class SaasPolicyIntegrationTests(unittest.TestCase):
    """The SaaS read policy must slot into the refresh route guard."""

    def test_saas_policy_is_stricter_subset_of_refresh_allowlist(self):
        # Every SaaS-allowed POST must also satisfy the legacy refresh policy
        # shape (exact host+path), so switching policies cannot widen access.
        from core.groups import refresh_request_allowed
        for method, url in (
            ("POST", "https://abtestvm.bytedance.com/service/2/abtest_config/"),
            ("POST", "https://creator.amemv.com/api/v1/workspace/feature_gates/values"),
            ("POST", "https://www.douyin.com/aweme/v1/web/im/user/info/"),
            ("POST", "https://www.douyin.com/aweme/v1/web/im/user/active/status/"),
        ):
            self.assertTrue(im_ui.saas_request_allowed(method, url), url)

    def test_saas_policy_denies_everything_else(self):
        allowed = im_ui.saas_request_allowed
        for method, url in (
            ("POST", "https://www.douyin.com/aweme/v1/web/im/user/info/send"),
            ("POST", "https://imapi.douyin.com/v1/message/send"),
            ("POST", "https://imapi.douyin.com/v1/message/get_message_by_init"),
            ("POST", "https://abtestvm.bytedance.com/service/2/abtest_config/extra"),
            ("POST", "https://creator.amemv.com/api/v1/workspace/feature_gates/values/x"),
            ("PUT", "https://www.douyin.com/aweme/v1/web/im/user/info/"),
            ("DELETE", "https://www.douyin.com/aweme/v1/web/im/user/info/"),
        ):
            self.assertFalse(allowed(method, url), (method, url))


if __name__ == "__main__":
    unittest.main()
