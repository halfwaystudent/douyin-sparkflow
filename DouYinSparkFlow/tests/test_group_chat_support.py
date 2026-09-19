import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from core import protocol_dispatch, streak_state, tasks

NOW = datetime(2026, 9, 15, 11, 0, tzinfo=timezone(timedelta(hours=8)))


class GroupChatStreakStateTests(unittest.TestCase):
    def test_group_chat_resolves_to_conversation_ref(self):
        account = {
            "username": "demo",
            "targets": ["Family Group"],
            "protocol_targets_cache": [
                {
                    "nickname": "Family Group",
                    "conversationId": "group-conv-123",
                    "isGroup": True,
                }
            ],
        }

        target_ref = streak_state.resolve_target_ref(account, "Family Group")
        self.assertEqual("conversation:group-conv-123", target_ref)

    def test_group_chat_rename_preserves_stable_target_ref(self):
        account = {
            "username": "demo",
            "targets": ["Family Group Old"],
            "protocol_targets_cache": [
                {
                    "nickname": "Family Group Old",
                    "conversationId": "group-conv-123",
                    "isGroup": True,
                }
            ],
        }
        first_ref = streak_state.resolve_target_ref(account, "Family Group Old")

        account["targets"] = ["Family Group New"]
        account["protocol_targets_cache"] = [
            {
                "nickname": "Family Group New",
                "conversationId": "group-conv-123",
                "isGroup": True,
            }
        ]
        second_ref = streak_state.resolve_target_ref(account, "Family Group New")

        self.assertEqual("conversation:group-conv-123", first_ref)
        self.assertEqual(first_ref, second_ref)

    def test_group_chat_confirmed_streak_record(self):
        account = {
            "username": "demo",
            "targets": ["Family Group"],
            "target_refs": {"Family Group": "conversation:group-conv-123"},
            "message_history": {
                "Family Group": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }
        streak_state.reconcile_account(account, NOW)

        state = streak_state.target_state(account, "Family Group", NOW)
        self.assertEqual("send_confirmed", state["status"])
        self.assertEqual("conversation:group-conv-123", state["targetRef"])
        self.assertTrue(streak_state.is_send_confirmed(account, "Family Group", NOW))


class GroupChatProtocolDispatchTests(unittest.TestCase):
    def test_build_protocol_target_identities_includes_conversation_id_for_group(self):
        account = {
            "username": "demo",
            "protocol_targets_cache": [
                {
                    "nickname": "Group 1",
                    "conversationId": "conv-999",
                    "isGroup": True,
                },
                {
                    "nickname": "Friend 1",
                    "secUid": "sec-abc",
                    "peerUserId": "1002",
                },
            ],
        }

        messages = {"Group 1": "hi group", "Friend 1": "hi friend"}
        identities = protocol_dispatch._build_protocol_target_identities(account, messages)

        self.assertIn("Group 1", identities)
        self.assertEqual("conv-999", identities["Group 1"].get("conversationId"))

        self.assertIn("Friend 1", identities)
        self.assertEqual("sec-abc", identities["Friend 1"].get("secUid"))
        self.assertEqual("1002", identities["Friend 1"].get("peerUserId"))


class GroupChatTasksTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_all_or_groups_tab_clicks_first_visible(self):
        page = MagicMock()
        mock_sub = MagicMock()
        page.locator.return_value = mock_sub

        mock_btn = MagicMock()
        mock_btn.count = AsyncMock(return_value=1)
        mock_btn_item = MagicMock()
        mock_btn_item.is_visible = AsyncMock(return_value=True)
        mock_btn_item.click = AsyncMock()
        mock_btn.nth = MagicMock(return_value=mock_btn_item)

        page.get_by_text = MagicMock(return_value=mock_btn)
        mock_sub.get_by_text = MagicMock(return_value=mock_btn)

        with patch("core.tasks._dismiss_non_login_dialogs", new=AsyncMock()):
            result = await tasks._open_all_or_groups_tab(page, "demo")
            self.assertTrue(result)
            mock_btn_item.click.assert_awaited()

    async def test_scroll_and_select_switches_tab_for_group_target(self):
        class Element:
            def __init__(self, name):
                self.name = name

            async def click(self):
                return None

        group_elem = Element("Group Spark")

        class Locator:
            def __init__(self, elements):
                self.elements = elements

            async def all(self):
                return self.elements

        async def extract_record(element):
            return {
                "visibleName": element.name,
                "normalizedName": tasks._normalize_target_name(element.name),
                "stableKeys": [],
            }

        switched = False

        async def fake_open_all_tab(*args, **kwargs):
            nonlocal switched
            switched = True
            return True

        async def fake_first_non_empty_locator(page, selectors):
            if switched:
                return "selector", Locator([group_elem])
            return "selector", Locator([])

        async def fake_selector_visible(page, selectors):
            if not switched:
                return "没有更多" in " ".join(selectors)
            return False

        generator = tasks.scroll_and_select_user(
            Mock(evaluate=AsyncMock()),
            {"username": "demo", "unique_id": "1001"},
            "demo",
            ["Group Spark"],
            {"maxScanSeconds": 60, "idleScanSeconds": 10, "scrollStepPx": 100, "scrollDelaySeconds": 0},
        )

        with patch.object(tasks, "_open_friends_tab", new=AsyncMock()), \
             patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("selector", Locator([])))), \
             patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)), \
             patch.object(tasks, "_first_non_empty_locator", side_effect=fake_first_non_empty_locator), \
             patch.object(tasks, "_extract_friend_record", side_effect=extract_record), \
             patch.object(tasks, "_selector_visible", side_effect=fake_selector_visible), \
             patch.object(tasks, "_open_all_or_groups_tab", side_effect=fake_open_all_tab), \
             patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("scroll", object()))), \
             patch.object(tasks, "_persist_friend_index"), \
             patch.object(tasks.asyncio, "sleep", new=AsyncMock()):

            try:
                selected = await anext(generator)
            finally:
                await generator.aclose()

        self.assertTrue(switched)
        self.assertEqual("Group Spark", selected)

    async def test_scroll_and_select_missing_target_exits_gracefully(self):
        class Locator:
            async def all(self):
                return []

        generator = tasks.scroll_and_select_user(
            Mock(evaluate=AsyncMock()),
            {"username": "demo", "unique_id": "1001"},
            "demo",
            ["Nonexistent Target"],
            {"maxScanSeconds": 60, "idleScanSeconds": 10, "scrollStepPx": 100, "scrollDelaySeconds": 0},
        )

        async def fake_selector_visible(page, selectors):
            return "没有更多" in " ".join(selectors)

        with patch.object(tasks, "_open_friends_tab", new=AsyncMock()), \
             patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("selector", Locator()))), \
             patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)), \
             patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("selector", Locator()))), \
             patch.object(tasks, "_selector_visible", side_effect=fake_selector_visible), \
             patch.object(tasks, "_open_all_or_groups_tab", new=AsyncMock(return_value=True)), \
             patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("scroll", object()))), \
             patch.object(tasks, "_persist_friend_index"), \
             patch.object(tasks.asyncio, "sleep", new=AsyncMock()):

            results = []
            async for target in generator:
                results.append(target)

            self.assertEqual([], results)


if __name__ == "__main__":
    unittest.main()
