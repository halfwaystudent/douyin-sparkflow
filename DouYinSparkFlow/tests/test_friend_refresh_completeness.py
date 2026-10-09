import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from core import friends as friends_module
from core import browser as browser_module
from scripts import clean_friend_junk


class _TextNode:
    def __init__(self, text):
        self._text = text

    async def count(self):
        return 1

    async def inner_text(self, timeout=None):
        return self._text


class _MissingNode:
    async def count(self):
        return 0

    async def inner_text(self, timeout=None):
        raise RuntimeError("no nickname node")


class _ElementLocator:
    """What ``row.locator(selector)`` returns in these tests."""

    def __init__(self, name):
        self._name = name

    @property
    def first(self):
        return _TextNode(self._name) if self._name is not None else _MissingNode()


class _FakeRow:
    """A chat row: a nickname node, or only a timestamp and message preview."""

    def __init__(self, name=None):
        self.name = name

    def locator(self, selector):
        del selector
        return _ElementLocator(self.name)


class _FakeRowLocator:
    def __init__(self, rows):
        self._rows = list(rows)

    async def all(self):
        return list(self._rows)

    async def count(self):
        return len(self._rows)


class _FakePageLocator:
    def __init__(self, handle=None):
        self._handle = handle

    @property
    def first(self):
        return self

    def nth(self, index):
        del index
        return self

    async def count(self):
        return 1 if self._handle is not None else 0

    async def element_handle(self, timeout=None):
        del timeout
        return self._handle


class _ScrollHandle:
    def __init__(self):
        self.top = 0


class _FakeScanPage:
    def __init__(self, handle, scroll_moves=True, scrollable=True):
        self._handle = handle
        self._scroll_moves = scroll_moves
        self._scrollable = scrollable

    def locator(self, selector):
        if selector in friends_module.SCROLLABLE_FRIENDS_SELECTORS:
            return _FakePageLocator(self._handle)
        return _FakePageLocator()

    async def evaluate(self, script, handle=None):
        if "clientHeight" in script:
            height = 4000 if self._scrollable else 600
            return {"clientHeight": 600, "scrollHeight": height}
        if "+=" in script:
            if self._scroll_moves:
                handle.top += friends_module.SCROLL_STEP_PX
            return None
        return handle.top


def _scan(
    rows,
    *,
    no_more=False,
    scroll_moves=True,
    rows_present=True,
    scrollable=True,
):
    handle = _ScrollHandle()
    page = _FakeScanPage(handle, scroll_moves=scroll_moves, scrollable=scrollable)
    row_locator = _FakeRowLocator(rows or [])

    async def fake_wait_for_chat_or_login(page_arg, timeout_seconds=None):
        return None

    async def fake_click_friends_tab(page_arg):
        return None

    async def fake_dismiss_dialogs(page_arg):
        return False

    async def fake_wait_for_rows(page_arg, timeout_seconds=None):
        return ("rows", row_locator) if rows_present else ("", None)

    async def fake_first_visible(page_arg, selectors):
        if selectors is friends_module.FRIEND_ROW_SELECTORS:
            return "rows", row_locator
        if selectors is friends_module.NO_MORE_SELECTORS:
            if no_more:
                return "no-more", _FakePageLocator(handle)
            return "", None
        return "", None

    with (
        patch.object(
            friends_module, "_wait_for_chat_or_login", fake_wait_for_chat_or_login
        ),
        patch.object(friends_module, "_click_friends_tab", fake_click_friends_tab),
        patch.object(friends_module, "_dismiss_non_login_dialogs", fake_dismiss_dialogs),
        patch.object(friends_module, "_wait_for_friend_rows_or_empty", fake_wait_for_rows),
        patch.object(friends_module, "_first_visible_locator", fake_first_visible),
        patch.object(friends_module, "SCROLL_SETTLE_SECONDS", 0),
    ):
        return asyncio.run(friends_module.collect_friend_names(page))


class FriendNameReadTests(unittest.TestCase):
    def test_row_without_a_nickname_node_yields_no_name(self):
        # The row still renders "16:23" and a preview; neither is a nickname.
        self.assertEqual("", asyncio.run(friends_module._read_friend_name(_FakeRow())))

    def test_row_with_a_nickname_node_yields_that_node(self):
        self.assertEqual(
            "林恩",
            asyncio.run(friends_module._read_friend_name(_FakeRow("林恩"))),
        )


class FriendScanCompletenessTests(unittest.TestCase):
    def test_only_rows_with_a_nickname_node_become_friends(self):
        result = _scan(
            [_FakeRow("林恩"), _FakeRow(), _FakeRow("抹茶叔叔")],
            no_more=True,
        )

        self.assertEqual(["林恩", "抹茶叔叔"], list(result))
        self.assertNotIn("16:23", list(result))

    def test_end_of_list_marker_marks_the_scan_complete(self):
        result = _scan([_FakeRow("林恩")], no_more=True)

        self.assertTrue(result.complete)
        self.assertEqual("", result.reason)

    def test_empty_list_with_marker_marks_the_scan_complete(self):
        result = _scan([], rows_present=False)

        self.assertTrue(result.complete)
        self.assertEqual([], list(result))

    def test_idle_scroll_is_incomplete(self):
        result = _scan([_FakeRow("林恩")])

        self.assertFalse(result.complete)
        self.assertEqual(["林恩"], list(result))
        self.assertIn("没有读到新的好友", result.reason)

    def test_stuck_scroll_is_incomplete(self):
        result = _scan([_FakeRow("林恩")], scroll_moves=False)

        self.assertFalse(result.complete)
        self.assertEqual(["林恩"], list(result))
        self.assertIn("滚动没有继续推进", result.reason)

    def test_list_with_nothing_below_the_viewport_is_complete(self):
        # A short list renders every row at once; the read provably reached the
        # end, so it must not be reported as an early stop.
        result = _scan([_FakeRow("林恩")], scrollable=False)

        self.assertTrue(result.complete)
        self.assertEqual(["林恩"], list(result))


class _Handle:
    def __init__(self, client_height, scroll_height):
        self.clientHeight = client_height
        self.scrollHeight = scroll_height


class _HandleSlot:
    def __init__(self, handle):
        self._handle = handle

    async def element_handle(self, timeout=None):
        del timeout
        return self._handle


class _HandleLocator:
    def __init__(self, handles):
        self._handles = list(handles)

    async def count(self):
        return len(self._handles)

    def nth(self, index):
        return _HandleSlot(self._handles[index])


class _HandlePage:
    def __init__(self, mapping):
        self._mapping = mapping

    def locator(self, selector):
        return _HandleLocator(self._mapping.get(selector, []))

    async def evaluate(self, script, handle=None):
        del script
        return {
            "clientHeight": handle.clientHeight,
            "scrollHeight": handle.scrollHeight,
        }


class FirstScrollableElementTests(unittest.TestCase):
    def test_skips_a_zero_height_grid_for_the_real_list(self):
        real = _Handle(679, 2100)
        page = _HandlePage({"[role=grid]": [_Handle(0, 0), real]})

        selector, handle = asyncio.run(
            browser_module.first_scrollable_element(page, ("[role=grid]",))
        )

        self.assertEqual("[role=grid]", selector)
        self.assertIs(real, handle)

    def test_reports_no_container_when_every_match_is_hidden(self):
        page = _HandlePage({"[role=grid]": [_Handle(0, 0)]})

        selector, handle = asyncio.run(
            browser_module.first_scrollable_element(page, ("[role=grid]", "[dead]"))
        )

        self.assertEqual("", selector)
        self.assertIsNone(handle)

    def test_prefers_a_container_with_content_below_the_fold(self):
        short = _Handle(679, 679)
        real = _Handle(679, 2100)
        page = _HandlePage({"[outer]": [short], "[real]": [real]})

        selector, handle = asyncio.run(
            browser_module.first_scrollable_element(page, ("[outer]", "[real]"))
        )

        self.assertEqual("[real]", selector)
        self.assertIs(real, handle)


class _StubPage:
    async def goto(self, *args, **kwargs):
        return None

    async def close(self):
        return None


class _StubPlaywright:
    async def stop(self):
        return None


class _StubContext:
    def __init__(self, auth_cookies=True):
        self.added = []
        self._auth_cookies = auth_cookies
        self.closed = False

    async def add_cookies(self, cookies):
        self.added.extend(cookies)

    async def cookies(self, *args, **kwargs):
        if self._auth_cookies:
            return [{"name": "sessionid", "value": "from-profile"}]
        return []

    async def new_page(self):
        return _StubPage()

    def set_default_navigation_timeout(self, value):
        return None

    def set_default_timeout(self, value):
        return None

    async def close(self):
        self.closed = True


PROFILE_CONFIG = {
    "enabled": True,
    "root": "/tmp/profiles",
    "seedCookiesWhenEmpty": True,
    "syncStoredCookiesBeforeRun": True,
    "refreshStoredCookiesAfterLogin": True,
}


class PersistentProfileRefreshTests(unittest.TestCase):
    """The refresh must share the sender's profile, lock and cookie policy."""

    def _run(self, profile_config, context, lock_calls):
        account = {
            "unique_id": "123",
            "username": "Tester",
            "cookies": [{"name": "sessionid", "value": "from-store"}],
        }

        async def fake_acquire(user, account_name, **kwargs):
            del user, kwargs
            lock_calls.append(("acquire", account_name))
            return ("guard", "lock-path", "token")

        def fake_release(handle, lock_path, token, account_name):
            lock_calls.append(("release", handle, lock_path, token, account_name))

        async def fake_context(*args, **kwargs):
            del args, kwargs
            return _StubPlaywright(), context, "/tmp/profiles/uid-123"

        with (
            patch.object(
                friends_module,
                "normalize_persistent_profile_config",
                return_value=profile_config,
            ),
            patch.object(
                friends_module, "acquire_browser_account_lock", fake_acquire
            ),
            patch.object(
                friends_module, "release_browser_account_lock", fake_release
            ),
            patch.object(
                friends_module, "get_persistent_browser_context", fake_context
            ),
            patch.object(
                friends_module,
                "collect_friend_names",
                new=AsyncMock(return_value=friends_module.FriendScanResult(
                    ["林恩"], complete=True
                )),
            ),
            patch.object(friends_module.asyncio, "sleep", new=AsyncMock()),
        ):
            return asyncio.run(
                friends_module._fetch_account_friends_once(account, "direct")
            )

    def test_holds_the_same_account_lock_as_the_sender(self):
        context = _StubContext()
        lock_calls = []

        result = self._run(PROFILE_CONFIG, context, lock_calls)

        self.assertEqual(["林恩"], list(result))
        self.assertEqual(["acquire", "release"], [call[0] for call in lock_calls])
        self.assertTrue(context.closed)

    def test_stored_cookies_are_synced_when_configured(self):
        context = _StubContext()

        self._run(PROFILE_CONFIG, context, [])

        self.assertEqual(
            [{"name": "sessionid", "value": "from-store"}], context.added
        )

    def test_profile_session_is_kept_when_sync_is_disabled(self):
        context = _StubContext(auth_cookies=True)
        config = dict(PROFILE_CONFIG, syncStoredCookiesBeforeRun=False)

        self._run(config, context, [])

        self.assertEqual([], context.added)

    def test_profile_is_seeded_only_when_it_has_no_session(self):
        empty_context = _StubContext(auth_cookies=False)
        config = dict(PROFILE_CONFIG, syncStoredCookiesBeforeRun=False)

        self._run(config, empty_context, [])

        self.assertEqual(
            [{"name": "sessionid", "value": "from-store"}], empty_context.added
        )


class IncompleteScanFieldTests(unittest.TestCase):
    def test_timeout_and_stalled_reads_share_one_classification(self):
        from webui import app as app_module

        fields = app_module._incomplete_scan_fields("读取超时，超过 300 秒")

        self.assertEqual("incomplete_scan", fields["category"])
        self.assertTrue(fields["retryable"])
        self.assertIn("读取超时，超过 300 秒", fields["error"])
        self.assertIn("已保留上一次的好友数据", fields["error"])


class FriendRefreshLoggingTests(unittest.TestCase):
    def test_route_log_names_account_route_count_and_completeness(self):
        account = {
            "unique_id": "123",
            "username": "Tester",
            "cookies": [{"name": "sessionid", "value": "x"}],
        }

        async def fake_once(account_arg, network_mode, **kwargs):
            del account_arg, network_mode, kwargs
            return friends_module.FriendScanResult(["林恩"], complete=True)

        with (
            patch.object(
                friends_module, "douyin_network_modes", return_value=("direct",)
            ),
            patch.object(friends_module, "_fetch_account_friends_once", fake_once),
            self.assertLogs("app", level="INFO") as captured,
        ):
            result = asyncio.run(friends_module.fetch_account_friends(account))

        self.assertEqual(["林恩"], list(result))
        line = "\n".join(captured.output)
        for fragment in ("account=Tester", "route=direct", "count=1", "complete=True"):
            self.assertIn(fragment, line)


class FriendJunkCleanupTests(unittest.TestCase):
    def test_predicate_matches_times_dates_and_counters_only(self):
        for value in (
            "16:23",
            "9:05",
            "11:01:02",
            "07-21",
            "2026-10-08",
            "9月30日",
            "1",
            "42",
            "昨天",
            "刚刚",
        ):
            self.assertTrue(clean_friend_junk.looks_like_non_name(value), value)

        for value in ("林恩", "🌈Ethereals", "R a m p a n t", "srx666", ""):
            self.assertFalse(clean_friend_junk.looks_like_non_name(value), value)

    def test_clean_account_drops_junk_and_keeps_real_entries(self):
        account = {
            "friends_cache": ["16:23", "林恩", "1", "抹茶叔叔"],
            "friend_index": {
                "林恩": {"visibleName": "林恩", "stableKeys": ["secUid:1"]},
                "07-21": {"visibleName": "07-21"},
                "srx666": {"visibleName": "srx666"},
            },
        }

        removed_cache, removed_index = clean_friend_junk.clean_account(account)

        self.assertEqual(["16:23", "1"], removed_cache)
        self.assertEqual(["07-21"], removed_index)
        self.assertEqual(["林恩", "抹茶叔叔"], account["friends_cache"])
        self.assertEqual({"林恩", "srx666"}, set(account["friend_index"]))
        self.assertEqual(
            ["secUid:1"],
            account["friend_index"]["林恩"]["stableKeys"],
        )


if __name__ == "__main__":
    unittest.main()
