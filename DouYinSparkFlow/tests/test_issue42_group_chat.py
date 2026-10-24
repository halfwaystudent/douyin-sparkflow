import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from core import friend_identity, protocol_dispatch, streak_state, tasks
from utils import config
from webui import app as web_app


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

    def get_by_text(self, name, exact=False):
        if name != self.name or not exact:
            raise AssertionError((name, exact))
        return self

    async def count(self):
        return 1


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
    def _run_group_send_until_press(
        self,
        press_side_effect,
        update_side_effect=None,
        update_handler=None,
        readback_handler=None,
        bound_id=None,
        verify_side_effect=None,
    ):
        """Exercise the production _do_user_task_locked send path through Enter."""
        user = {
            "username": "demo",
            "unique_id": "1001",
            "cookies": [],
            "targets": ["群聊：目标群"],
        }
        if bound_id:
            user["group_target_bindings"] = {"群聊：目标群": bound_id}
        self.verify = AsyncMock(side_effect=verify_side_effect or (lambda *_a, **_k: (True, "")))
        page = Mock(goto=AsyncMock(), close=AsyncMock())
        context = Mock(
            new_page=AsyncMock(return_value=page),
            add_cookies=AsyncMock(),
            close=AsyncMock(),
        )
        browser = Mock(new_context=AsyncMock(return_value=context))
        chat_input = Mock(type=AsyncMock(), press=AsyncMock(side_effect=press_side_effect))
        collector = Mock()
        collector.attach.return_value = collector

        async def selected(*_args, **_kwargs):
            yield "群聊：目标群"

        def persist(mutate, **_kwargs):
            if update_side_effect:
                raise update_side_effect
            if update_handler:
                return update_handler(mutate, user, **_kwargs)
            return mutate([user])[0]

        with (
            patch.object(tasks, "_sleep_with_log", new=AsyncMock()),
            patch.object(tasks, "retry_operation", new=AsyncMock()),
            patch.object(tasks, "ensure_not_login_required", new=AsyncMock()),
            patch.object(tasks, "scroll_and_select_user", new=selected),
            patch.object(tasks, "locate_chat_input", new=AsyncMock(return_value=(chat_input, "input"))),
            patch.object(tasks, "build_message", return_value="hello"),
            patch.object(tasks, "snapshot_last_own_message", new=AsyncMock(return_value=None)),
            patch.object(tasks, "_mark_browser_target_in_flight", return_value=True),
            patch.object(tasks, "save_debug_artifacts", new=AsyncMock()),
            patch.object(tasks, "start_im_send_observer", new=AsyncMock()),
            patch.object(tasks, "_detect_send_failure_indicator", new=AsyncMock(return_value=None)),
            patch.object(tasks, "detect_message_already_sent", new=AsyncMock(return_value=(False, "not sent"))),
            patch.object(tasks, "_page_has_login_required_prompt", new=AsyncMock(return_value=(False, ""))),
            patch.object(tasks, "update_user_data", side_effect=persist),
            patch.object(
                tasks,
                "get_userData",
                side_effect=readback_handler or (lambda **_kwargs: [user]),
            ),
            patch.object(tasks.friend_identity, "UserDetailCollector", return_value=collector),
            patch.object(tasks, "_verify_open_group_conversation", new=self.verify),
        ):
            try:
                asyncio.run(
                    tasks._do_user_task_locked(
                        browser, user,
                        {"shuffleTargets": False, "accountStartDelaySecondsMin": 0,
                         "accountStartDelaySecondsMax": 0, "messageIntervalSecondsMin": 0,
                         "messageIntervalSecondsMax": 0},
                        {"enabled": False}, SCAN_CONFIG, "demo", "direct",
                    )
                )
            except (asyncio.CancelledError, OSError):
                pass
        return user, chat_input

    def test_enter_timeout_keeps_persisted_unknown_barrier(self):
        user, chat_input = self._run_group_send_until_press(asyncio.TimeoutError("dispatched"))

        self.assertEqual(1, chat_input.press.await_count)
        self.assertEqual(
            streak_state.STATE_SENT_UNVERIFIED,
            streak_state.target_state(user, "群聊：目标群")["status"],
        )

    def test_enter_cancellation_keeps_persisted_unknown_barrier(self):
        user, chat_input = self._run_group_send_until_press(asyncio.CancelledError())

        self.assertEqual(1, chat_input.press.await_count)
        self.assertEqual(
            streak_state.STATE_SENT_UNVERIFIED,
            streak_state.target_state(user, "群聊：目标群")["status"],
        )

    def test_bound_group_switched_away_before_enter_is_never_sent(self):
        """Typing and barrier writes are awaits: the open chat can change under us."""
        outcomes = iter([(True, ""), (False, "active row id mismatch")])
        user, chat_input = self._run_group_send_until_press(
            None,
            bound_id="7691234567890123490",
            verify_side_effect=lambda *_a, **_k: next(outcomes),
        )

        chat_input.press.assert_not_awaited()
        self.assertEqual(2, self.verify.await_count)
        # The barrier was already durable when the switch was detected: it stays
        # a non-resendable unknown, never a retryable failure.
        state = streak_state.target_state(user, "群聊：目标群")
        self.assertEqual(streak_state.STATE_SENT_UNVERIFIED, state["status"])
        self.assertFalse(streak_state.fallback_eligible(user, "群聊：目标群"))

    def test_bound_group_mismatch_before_barrier_writes_nothing_and_sends_nothing(self):
        user, chat_input = self._run_group_send_until_press(
            None,
            bound_id="7691234567890123490",
            verify_side_effect=lambda *_a, **_k: (False, "header name mismatch"),
        )

        chat_input.press.assert_not_awaited()
        self.assertEqual(1, self.verify.await_count)
        self.assertNotEqual(
            streak_state.STATE_SENT_UNVERIFIED,
            (streak_state.target_state(user, "群聊：目标群") or {}).get("status"),
        )

    def test_bound_group_verifies_after_barrier_and_immediately_before_enter(self):
        order = []
        real_persist = tasks._persist_browser_pre_enter_unknown

        def persist_barrier(*args, **kwargs):
            order.append("barrier")
            return real_persist(*args, **kwargs)

        def verify(*_a, **_k):
            order.append("verify")
            return (True, "")

        with patch.object(tasks, "_persist_browser_pre_enter_unknown", side_effect=persist_barrier):
            _user, chat_input = self._run_group_send_until_press(
                lambda *_a, **_k: order.append("enter"),
                bound_id="7691234567890123490",
                verify_side_effect=verify,
            )

        self.assertEqual(["verify", "barrier", "verify", "enter"], order)
        self.assertEqual(1, chat_input.press.await_count)

    def test_unbound_group_send_path_does_not_require_identity_verifier(self):
        _user, chat_input = self._run_group_send_until_press(None)

        self.assertEqual(0, self.verify.await_count)
        self.assertEqual(1, chat_input.press.await_count)

    def test_storage_failure_prevents_enter_dispatch(self):
        _user, chat_input = self._run_group_send_until_press(
            None, OSError("ledger unavailable")
        )

        chat_input.press.assert_not_awaited()

    def test_group_rename_notice_is_persisted_and_cleared_in_users_data(self):
        user = {"username": "demo", "unique_id": "1001", "targets": ["群聊：旧群"]}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "usersData.json"
            path.write_text(json.dumps([user], ensure_ascii=False), encoding="utf-8")
            with (
                # On GitHub Actions accounts come from $USER_DATA; this test
                # needs the file-backed store it writes to.
                patch.object(config, "get_environment", return_value=config.Environment.LOCAL),
                patch.object(config, "users_data_path", return_value=path),
                patch.object(config, "userData", None),
            ):
                tasks._persist_group_target_rename_notice(user, "群聊：旧群", "新群")
                persisted = config.get_userData()[0]
                self.assertEqual("renamed", persisted["group_target_notices"]["群聊：旧群"]["kind"])
                self.assertEqual("新群", persisted["group_target_notices"]["群聊：旧群"]["currentName"])

                tasks._persist_group_target_rename_notice(user, "群聊：旧群", "旧群")
                self.assertNotIn("group_target_notices", config.get_userData()[0])

    def test_group_rename_notice_persistence_failure_does_not_raise(self):
        with patch.object(tasks, "update_user_data", side_effect=OSError("disk unavailable")):
            tasks._persist_group_target_rename_notice(
                {"username": "demo", "unique_id": "1001"}, "群聊：旧群", "新群"
            )

    def test_readback_missing_or_overwritten_barrier_prevents_enter_dispatch(self):
        """A write result is not proof that the durable barrier still exists."""
        for replacement in ([], [{"username": "demo", "unique_id": "1001", "targets": ["群聊：目标群"]}]):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "usersData.json"
                wrote_initial = False

                def persist_then_replace(mutate, user, **kwargs):
                    nonlocal wrote_initial
                    if not wrote_initial:
                        path.write_text(
                            json.dumps([copy.deepcopy(user)], ensure_ascii=False),
                            encoding="utf-8",
                        )
                        wrote_initial = True
                    result = config.update_user_data(mutate, **kwargs)
                    self.assertEqual(
                        streak_state.STATE_SENT_UNVERIFIED, result.get("status")
                    )
                    path.write_text(
                        json.dumps(replacement, ensure_ascii=False), encoding="utf-8"
                    )
                    return result

                with (
                    patch.object(config, "users_data_path", return_value=path),
                    patch.object(config, "userData", None),
                ):
                    _user, chat_input = self._run_group_send_until_press(
                        None,
                        update_handler=persist_then_replace,
                        readback_handler=config.get_userData,
                    )

                chat_input.press.assert_not_awaited()

    def test_unknown_barrier_has_zero_send_count_in_all_retry_planners(self):
        now = tasks.datetime.now(tasks._schedule_timezone())
        target = "群聊：目标群"
        user = {"username": "demo", "unique_id": "1001", "targets": [target]}
        streak_state.mark_sent_unverified(user, target, now=now, detail="pre_enter_outcome_unknown_barrier")
        window = {"enabled": True, "startHour": 0, "endHour": 24, "scheduleIntervalMinutes": 20}

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(tasks, "_is_fallback_run", return_value=False),
        ):
            regular_due, _, _, _ = tasks._select_due_targets(user, window, now)
        with patch.object(tasks.streak_state, "schedule_phase", return_value="fallback"):
            fallback_due, _, _, _ = tasks._select_due_targets(user, window, now)
        failed_only = tasks._pending_failed_targets(user, now)
        unsent_only, _skipped = tasks._pending_unsent_targets(user, now)

        send_count = sum(map(len, (regular_due, fallback_due, failed_only, unsent_only)))
        self.assertEqual(0, send_count)

    def test_only_explicit_server_rejection_releases_unknown_barrier(self):
        now = tasks.datetime.now(tasks._schedule_timezone())
        target = "群聊：目标群"
        user = {"username": "demo", "unique_id": "1001", "targets": [target]}
        other = {"username": "other", "unique_id": "1002", "targets": [target]}
        streak_state.mark_sent_unverified(user, target, now=now)

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=lambda mutate, **_kwargs: mutate([user, other])[0],
        ):
            tasks._persist_browser_send_failure(
                user, target, "hello", "unknown", "generic failure",
                now.isoformat(timespec="seconds"),
            )
            self.assertEqual(
                streak_state.STATE_SENT_UNVERIFIED,
                streak_state.target_state(user, target, now)["status"],
            )
            tasks._persist_browser_send_failure(
                user, target, "hello", "send_rejected", "server explicitly refused",
                now.isoformat(timespec="seconds"),
                override_sent_unverified=True,
            )

        self.assertEqual(
            streak_state.STATE_FAILED_TERMINAL,
            streak_state.target_state(user, target, now)["status"],
        )
        self.assertEqual("pending", streak_state.target_state(other, target, now)["status"])

    def test_form_dedupes_group_prefix_aliases_but_keeps_same_named_friend(self):
        self.assertEqual(
            ["同名会话", "群聊：同名会话"],
            web_app._split_target_entries(["同名会话\n群聊：同名会话\n群聊:同名会话"]),
        )

    def test_pre_enter_group_unknown_barrier_is_not_due_for_retry(self):
        now = tasks.datetime(2026, 10, 23, 20, 0, tzinfo=tasks._schedule_timezone())
        user = {"username": "demo", "targets": ["群聊：目标群"]}

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=lambda mutate, **_kwargs: mutate([user])[0],
        ), patch.object(tasks, "get_userData", return_value=[user]):
            persisted = tasks._persist_browser_pre_enter_unknown(
                user, "群聊：目标群", "hello", now.isoformat(timespec="seconds")
            )
        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(tasks, "_is_fallback_run", return_value=False),
        ):
            due, already_sent, pending, failures = tasks._select_due_targets(
                user,
                {"enabled": True, "startHour": 9, "endHour": 21, "scheduleIntervalMinutes": 20},
                now,
            )

        self.assertEqual([], due)
        self.assertTrue(persisted)
        self.assertEqual([], already_sent)
        self.assertEqual(["群聊：目标群"], [target for target, _ in pending])
        self.assertEqual([], failures)
        self.assertEqual(
            streak_state.STATE_SENT_UNVERIFIED,
            streak_state.target_state(user, "群聊：目标群", now)["status"],
        )

    def test_protocol_mode_sends_only_friends_and_browser_gets_groups(self):
        user = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["好友", "群聊：目标群", "群聊:目标群"],
        }

        protocol_users, browser_users = tasks._split_sender_modes(
            {"useProtocolSender": True}, [user]
        )

        self.assertEqual([["好友"]], [item["targets"] for item in protocol_users])
        self.assertEqual([["群聊：目标群"]], [item["targets"] for item in browser_users])
        self.assertIsNot(protocol_users[0], user)
        self.assertIsNot(browser_users[0], user)

    def test_protocol_identity_builder_fail_closes_group_targets(self):
        identities = protocol_dispatch._build_protocol_target_identities(
            {
                "protocol_targets_cache": [
                    {"nickname": "群聊：目标群", "secUid": "wrong-peer", "peerUserId": "1001"}
                ]
            },
            {"群聊：目标群": "hello"},
        )

        self.assertEqual({}, identities)

    def test_web_friend_binding_skips_group_namespace(self):
        account = {
            "targets": ["好友", "群聊：目标群"],
            "friend_uids": {
                "1001": {"nickname": "好友", "secUid": "sec-friend"},
                "2002": {"nickname": "群聊：目标群", "secUid": "sec-group"},
            },
        }

        result = friend_identity.apply_web_target_selection(account, [])

        self.assertEqual(["好友"], result["bound"])
        self.assertNotIn("群聊：目标群", result["unbound"])
        self.assertNotIn("群聊：目标群", account.get("friend_target_bindings", {}))

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

    def test_group_target_binding_lookup_resolves_ascii_and_fullwidth_aliases(self):
        account = {
            "group_target_bindings": {
                "群聊：目标群": "7691234567890123490",
            }
        }
        self.assertEqual(
            "7691234567890123490",
            tasks.get_bound_group_conversation_id(account, "群聊:目标群"),
        )
        self.assertIsNone(tasks.get_bound_group_conversation_id(account, "目标群"))

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
            patch.object(tasks, "_stable_single_screen_group_list", new=AsyncMock(return_value=("目标群",))),
            patch.object(tasks, "_confirm_group_chat_header", new=AsyncMock(return_value=True)),
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            selected = asyncio.run(
                _collect(tasks.scroll_and_select_group_targets(page, "demo", ["群聊：目标群"], SCAN_CONFIG))
            )

        self.assertEqual(["群聊：目标群"], selected)

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
            with self.assertRaisesRegex(tasks.FriendListIncompleteError, "single-screen"):
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
            patch.object(tasks, "_stable_single_screen_group_list", new=AsyncMock(return_value=("目标群", "目标群"))),
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
            patch.object(tasks, "_stable_single_screen_group_list", new=AsyncMock(return_value=())),
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

        async def select_group(page, account_name, targets, friend_scan_config, account=None):
            del page, account_name, friend_scan_config, account
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

    def test_bound_group_failure_keeps_later_group_and_friend_selection(self):
        friend = _Row("好友")
        locator = _Locator([friend])
        group_errors = []

        async def select_groups(*_args, errors=None, **_kwargs):
            errors.append({"target": "群聊：缺失A", "category": "group_not_found", "reason": "missing"})
            yield "群聊：存在B"

        with (
            patch.object(tasks, "scroll_and_select_group_targets", new=select_groups),
            patch.object(tasks, "_open_friends_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
            patch.object(tasks, "_first_non_empty_locator", new=AsyncMock(return_value=("rows", locator))),
            patch.object(tasks, "_extract_friend_record", side_effect=_record),
            patch.object(tasks, "_persist_friend_index"),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("", None))),
            patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
        ):
            async def select_two():
                generator = tasks.scroll_and_select_user(
                    Mock(evaluate=AsyncMock()), {"username": "demo", "unique_id": "1001"}, "demo",
                    ["群聊：缺失A", "群聊：存在B", "好友"], SCAN_CONFIG,
                    group_scan_errors=group_errors,
                )
                try:
                    return [await anext(generator), await anext(generator)]
                finally:
                    await generator.aclose()

            selected = asyncio.run(select_two())

        self.assertEqual(["群聊：存在B", "好友"], selected)
        self.assertEqual(
            [{"target": "群聊：缺失A", "category": "group_not_found", "reason": "missing"}], group_errors,
        )

    def test_group_failure_after_yield_only_records_unyielded_group_targets(self):
        friend = _Row("好友")
        locator = _Locator([friend])
        group_errors = []

        async def select_groups(*_args, **_kwargs):
            yield "群聊：已选群"
            raise tasks.FriendListIncompleteError("group list changed")

        with (
            patch.object(tasks, "scroll_and_select_group_targets", new=select_groups),
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
                        ["好友", "群聊：已选群", "群聊：未选群"], SCAN_CONFIG,
                        group_scan_errors=group_errors,
                    )
                )
            )

        self.assertEqual(["群聊：已选群", "好友"], selected)
        self.assertEqual(
            [{"target": "群聊：未选群", "category": "friend_list_incomplete", "reason": "group list changed"}],
            group_errors,
        )

    def test_mixed_same_name_targets_keep_separate_daily_ledgers_and_dedupe_group_aliases(self):
        now = tasks.datetime(2026, 10, 23, 20, 0, tzinfo=tasks._schedule_timezone())
        user = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["同名会话", "群聊：同名会话", "群聊:同名会话"],
            "message_history": {
                "同名会话": {
                    "sentAt": "2026-10-23T12:00:00+00:00",
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                    "needsVerification": False,
                }
            },
        }
        window = {"enabled": True, "startHour": 9, "endHour": 21, "scheduleIntervalMinutes": 20}

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(tasks, "_is_fallback_run", return_value=False),
        ):
            due, already_sent, pending, queued_failures = tasks._select_due_targets(user, window, now)

        self.assertEqual(["同名会话"], already_sent)
        self.assertEqual(
            ["群聊：同名会话"],
            due + [target for target, _scheduled_at in pending],
        )
        self.assertEqual([], queued_failures)
        self.assertEqual(
            ["同名会话", "群聊：同名会话"],
            tasks._dedupe_group_target_aliases(user["targets"]),
        )

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
            patch.object(tasks, "_stable_single_screen_group_list", new=AsyncMock(return_value=("目标群",))),
            patch.object(tasks, "_confirm_group_chat_header", new=AsyncMock(return_value=True)),
        ):
            selected = asyncio.run(
                _collect(tasks.scroll_and_select_group_targets(page, "demo", ["群聊：目标群", "群聊:目标群"], SCAN_CONFIG))
            )

        self.assertEqual(["群聊：目标群"], selected)
        self.assertEqual(1, row.clicks)


if __name__ == "__main__":
    unittest.main()
