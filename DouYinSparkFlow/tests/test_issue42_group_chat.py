import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core import tasks


SCAN_CONFIG = {
    "maxScanSeconds": 60,
    "idleScanSeconds": 10,
    "scrollStepPx": 100,
    "scrollDelaySeconds": 0,
}


class _Row:
    def __init__(self, name, *, visible=True):
        self.name = name
        self.visible = visible
        self.clicks = 0

    async def is_visible(self, timeout=None):
        del timeout
        return self.visible

    async def click(self):
        self.clicks += 1


class _Locator:
    def __init__(self, rows):
        self.rows = rows

    async def all(self):
        return self.rows


async def _record(row):
    return {
        "visibleName": row.name,
        "normalizedName": row.name,
        "stableKeys": [],
    }


async def _collect(generator):
    return [target async for target in generator]


class GroupChatTargetTests(unittest.TestCase):
    def test_friend_tab_is_clicked_when_a_group_row_is_visible(self):
        page = Mock()
        page.locator.return_value = Mock()
        click = AsyncMock(return_value=True)

        with (
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock()) as existing_rows,
            patch.object(tasks, "_click_first_visible_locator", new=click),
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            result = asyncio.run(
                tasks._open_friends_tab(page, "demo", "fallback", ("group-row",))
            )

        self.assertEqual("clicked", result)
        existing_rows.assert_not_awaited()
        click.assert_awaited_once()

    def test_group_target_keeps_same_named_friend_in_a_separate_namespace(self):
        self.assertEqual(("group", "同名会话"), tasks.parse_chat_target("群聊：同名会话"))
        self.assertEqual(("friend", "同名会话"), tasks.parse_chat_target("同名会话"))
        self.assertNotEqual(
            tasks.target_match_key("群聊：同名会话"),
            tasks.target_match_key("同名会话"),
        )

    def test_group_tab_refuses_to_scan_when_selected_attribute_is_not_true(self):
        class Tab:
            async def is_visible(self, timeout=None):
                del timeout
                return True

            async def get_attribute(self, name):
                if name != "aria-selected":
                    raise AssertionError(name)
                return "false"

        class TabLocator:
            async def count(self):
                return 1

            def nth(self, index):
                if index != 0:
                    raise AssertionError(index)
                return Tab()

        page = Mock()
        page.get_by_role.return_value = TabLocator()
        page.locator.return_value = Mock()

        with (
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_click_first_visible_locator", new=AsyncMock(return_value=True)),
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(tasks.FriendListUnavailableError, "not active"):
                asyncio.run(tasks._open_groups_tab(page, "demo"))

    def test_group_scan_does_not_click_until_end_confirms_unique_target(self):
        row = _Row("目标群")
        locator = _Locator([row])

        with (
            patch.object(tasks, "_open_groups_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_extract_friend_record", side_effect=_record),
            patch.object(tasks, "_selector_visible", new=AsyncMock(return_value=False)),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("", None))),
        ):
            with self.assertRaisesRegex(tasks.FriendListIncompleteError, "group_list_incomplete"):
                asyncio.run(
                    _collect(
                        tasks.scroll_and_select_group_targets(
                            Mock(), "demo", ["群聊：目标群"], SCAN_CONFIG
                        )
                    )
                )

        self.assertEqual(0, row.clicks)

    def test_group_scan_resets_a_pre_scrolled_grid_before_reading_or_selecting(self):
        row = _Row("目标群")
        locator = _Locator([row])
        grid = object()
        page = Mock(evaluate=AsyncMock(return_value=True))

        with (
            patch.object(tasks, "_open_groups_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("grid", grid))),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_extract_friend_record", side_effect=_record),
            patch.object(tasks, "_selector_visible", new=AsyncMock(return_value="没有更多")),
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            selected = asyncio.run(
                _collect(tasks.scroll_and_select_group_targets(page, "demo", ["群聊：目标群"], SCAN_CONFIG))
            )

        self.assertEqual(["群聊：目标群"], selected)
        page.evaluate.assert_awaited_once_with(
            "(element) => { element.scrollTop = 0; return element.scrollTop === 0; }", grid
        )

    def test_group_scan_does_not_select_when_grid_reset_fails(self):
        row = _Row("目标群")
        locator = _Locator([row])
        page = Mock(evaluate=AsyncMock(side_effect=RuntimeError("grid detached")))

        with (
            patch.object(tasks, "_open_groups_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("grid", object()))),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_extract_friend_record", side_effect=_record),
            patch.object(tasks, "_selector_visible", new=AsyncMock(return_value="没有更多")),
        ):
            with self.assertRaisesRegex(tasks.FriendListIncompleteError, "reset"):
                asyncio.run(
                    _collect(tasks.scroll_and_select_group_targets(page, "demo", ["群聊：目标群"], SCAN_CONFIG))
                )

        self.assertEqual(0, row.clicks)

    def test_group_scan_rejects_duplicate_visible_names_without_clicking(self):
        first = _Row("目标群")
        second = _Row("目标群")
        locator = _Locator([first, second])
        page = Mock(evaluate=AsyncMock(return_value=True))

        with (
            patch.object(tasks, "_open_groups_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("grid", object()))),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_extract_friend_record", side_effect=_record),
            patch.object(tasks, "_selector_visible", new=AsyncMock(return_value="没有更多")),
        ):
            with self.assertRaisesRegex(tasks.FriendListIncompleteError, "ambiguous"):
                asyncio.run(
                    _collect(tasks.scroll_and_select_group_targets(page, "demo", ["群聊：目标群"], SCAN_CONFIG))
                )

        self.assertEqual((0, 0), (first.clicks, second.clicks))

    def test_group_scan_ignores_hidden_rows_returned_by_locator_all(self):
        hidden = _Row("目标群", visible=False)
        locator = _Locator([hidden])
        page = Mock(evaluate=AsyncMock(return_value=True))

        with (
            patch.object(tasks, "_open_groups_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("grid", object()))),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_extract_friend_record", side_effect=_record),
            patch.object(tasks, "_selector_visible", new=AsyncMock(return_value="没有更多")),
        ):
            selected = asyncio.run(
                _collect(tasks.scroll_and_select_group_targets(page, "demo", ["群聊：目标群"], SCAN_CONFIG))
            )

        self.assertEqual([], selected)
        self.assertEqual(0, hidden.clicks)

    def test_mixed_flow_passes_one_group_alias_and_keeps_friend_selection(self):
        friend = _Row("好友")
        locator = _Locator([friend])
        group_calls = []

        async def select_group(page, account_name, targets, friend_scan_config):
            del page, account_name, friend_scan_config
            group_calls.append(list(targets))
            yield targets[0]

        async def run():
            generator = tasks.scroll_and_select_user(
                Mock(evaluate=AsyncMock()),
                {"username": "demo", "unique_id": "1001"},
                "demo",
                ["好友", "群聊：目标群", "群聊:目标群"],
                SCAN_CONFIG,
                index_targets=["好友", "群聊：目标群", "群聊:目标群"],
            )
            try:
                return [await anext(generator), await anext(generator)]
            finally:
                await generator.aclose()

        with (
            patch.object(tasks, "scroll_and_select_group_targets", new=select_group),
            patch.object(tasks, "_open_friends_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_extract_friend_record", side_effect=_record),
            patch.object(tasks, "_persist_friend_index"),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("", None))),
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            selected = asyncio.run(run())

        self.assertEqual(["群聊：目标群", "好友"], selected)
        self.assertEqual([["群聊：目标群"]], group_calls)

    def test_unavailable_group_scan_keeps_scanning_friends_and_records_group_error(self):
        friend = _Row("好友")
        locator = _Locator([friend])
        group_errors = []

        with (
            patch.object(tasks, "scroll_and_select_group_targets", side_effect=tasks.FriendListUnavailableError("group tab missing")),
            patch.object(tasks, "_open_friends_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_extract_friend_record", side_effect=_record),
            patch.object(tasks, "_persist_friend_index"),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("", None))),
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            selected = asyncio.run(
                _collect(
                    tasks.scroll_and_select_user(
                        Mock(evaluate=AsyncMock()), {"username": "demo"}, "demo",
                        ["好友", "群聊：目标群"], SCAN_CONFIG,
                        group_scan_errors=group_errors,
                    )
                )
            )

        self.assertEqual(["好友"], selected)
        self.assertEqual(
            [{"target": "群聊：目标群", "category": "friend_list_unavailable", "reason": "group tab missing"}],
            group_errors,
        )

    def test_incomplete_group_scan_keeps_scanning_friends(self):
        friend = _Row("好友")
        locator = _Locator([friend])

        with (
            patch.object(tasks, "scroll_and_select_group_targets", side_effect=tasks.FriendListIncompleteError("group scan timed out")),
            patch.object(tasks, "_open_friends_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_extract_friend_record", side_effect=_record),
            patch.object(tasks, "_persist_friend_index"),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("", None))),
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            selected = asyncio.run(
                _collect(
                    tasks.scroll_and_select_user(
                        Mock(evaluate=AsyncMock()), {"username": "demo"}, "demo",
                        ["好友", "群聊：目标群"], SCAN_CONFIG,
                    )
                )
            )

        self.assertEqual(["好友"], selected)

    def test_group_scan_failure_uses_group_ledger_without_friend_not_found_rewrite(self):
        page = Mock(goto=AsyncMock(), close=AsyncMock())
        context = Mock(
            new_page=AsyncMock(return_value=page),
            add_cookies=AsyncMock(),
            close=AsyncMock(),
        )
        browser = Mock(new_context=AsyncMock(return_value=context))
        collector = Mock()
        collector.attach.return_value = collector

        async def select_with_group_failure(*args, group_scan_errors=None, **kwargs):
            del args, kwargs
            group_scan_errors.append(
                {
                    "target": "群聊：目标群",
                    "category": "friend_list_incomplete",
                    "reason": "group scan timed out",
                }
            )
            if False:
                yield None

        failures = Mock()
        with (
            patch.object(tasks, "_sleep_with_log", new=AsyncMock()),
            patch.object(tasks, "retry_operation", new=AsyncMock()),
            patch.object(tasks, "ensure_not_login_required", new=AsyncMock()),
            patch.object(tasks, "_friend_index_complete_today", return_value=True),
            patch.object(tasks, "scroll_and_select_user", new=select_with_group_failure),
            patch.object(tasks, "_persist_browser_send_failure", new=failures),
            patch.object(tasks.friend_identity, "UserDetailCollector", return_value=collector),
        ):
            asyncio.run(
                tasks._do_user_task_locked(
                    browser,
                    {"username": "demo", "unique_id": "1001", "targets": ["好友", "群聊：目标群"], "cookies": []},
                    {
                        "shuffleTargets": False,
                        "accountStartDelaySecondsMin": 0,
                        "accountStartDelaySecondsMax": 0,
                        "messageIntervalSecondsMin": 0,
                        "messageIntervalSecondsMax": 0,
                    },
                    {"enabled": False},
                    SCAN_CONFIG,
                    "demo",
                    "direct",
                )
            )

        calls = failures.call_args_list
        self.assertEqual("群聊：目标群", calls[0].args[1])
        self.assertEqual("friend_list_incomplete", calls[0].args[3])
        self.assertEqual("好友", calls[1].args[1])
        self.assertEqual("friend_not_found", calls[1].args[3])
        self.assertEqual(2, len(calls))

    def test_group_prefix_aliases_select_once_and_return_first_configured_target(self):
        row = _Row("目标群")
        locator = _Locator([row])
        page = Mock(evaluate=AsyncMock(return_value=True))

        with (
            patch.object(tasks, "_open_groups_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("grid", object()))),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_extract_friend_record", side_effect=_record),
            patch.object(tasks, "_selector_visible", new=AsyncMock(return_value="没有更多")),
        ):
            selected = asyncio.run(
                _collect(tasks.scroll_and_select_group_targets(page, "demo", ["群聊：目标群", "群聊:目标群"], SCAN_CONFIG))
            )

        self.assertEqual(["群聊：目标群"], selected)
        self.assertEqual(1, row.clicks)


if __name__ == "__main__":
    unittest.main()
