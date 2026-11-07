"""Sender-boundary tests; synthetic objects, no network."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from core import tasks, friend_chat_guard
from tests.test_browser_stage_timeouts import flow


class FriendGuardCategoryTests(unittest.TestCase):
    def test_known_identity_conflict_is_not_reported_as_unreadable(self):
        mismatch = friend_chat_guard.FriendIdentityError(
            'friend_identity_unavailable: friend_active_identity_mismatch')
        self.assertEqual(mismatch.category, 'friend_identity_mismatch')
        unreadable = friend_chat_guard.FriendIdentityError(
            'friend_identity_unavailable: friend_header_mismatch')
        self.assertEqual(unreadable.category, 'friend_identity_unavailable')

    def test_uid_only_binding_is_rejected_instead_of_degrading(self):
        account = {'target_uid_bindings': {'friend': {'uid': '10001'}}}
        with self.assertRaises(friend_chat_guard.FriendIdentityError) as caught:
            friend_chat_guard._expected(account, 'friend')
        self.assertIn('friend_binding_incomplete', str(caught.exception))
        self.assertEqual(caught.exception.category, 'friend_identity_unavailable')

    def test_incomplete_binding_selection_returns_skip_before_reading_the_row(self):
        row = AsyncMock()
        account = {'target_uid_bindings': {'friend': {'uid': '10001'}}}
        async def exercise():
            try:
                result = await friend_chat_guard.select_friend(object(), row, account, 'friend')
            except friend_chat_guard.FriendIdentityError as exc:
                self.fail('selection must return a per-target skip, not abort the account: ' + str(exc))
            self.assertEqual(result, (False, 'friend_binding_incomplete'))
            row.evaluate.assert_not_awaited()
        asyncio.run(exercise())

    def test_complete_binding_is_accepted(self):
        account = {'target_uid_bindings': {'friend': {'uid': '10001', 'secUid': 'sec-1'}}}
        self.assertEqual(friend_chat_guard._expected(account, 'friend'),
                         {'peer': '10001', 'sec': 'sec-1'})


class FriendGuardSenderTests(unittest.TestCase):
    def test_identity_failure_stops_the_run_before_the_next_target(self):
        with flow(identity_guard=True) as (user, page, context, browser, chat, release, run):
            attempted = []
            async def verify(page, account, target, **kw):
                attempted.append(target)
                return False, 'friend_active_identity_mismatch'
            with patch.object(friend_chat_guard, 'verify_friend', side_effect=verify), \
                 patch.object(tasks, 'detect_message_already_sent', AsyncMock()) as recovery:
                asyncio.run(run())
            self.assertEqual(attempted, ['first'])
            chat.press.assert_not_awaited()
            recovery.assert_not_awaited()
            self.assertEqual(user['account_failure']['category'], 'friend_identity_mismatch')

    def test_verify_checkpoint_verify_enter_call_order(self):
        with flow(identity_guard=True) as (user, page, context, browser, chat, release, run):
            events = []
            original_mark = tasks._mark_browser_target_in_flight
            original_barrier = tasks._persist_browser_pre_enter_unknown
            def mark(*args, **kw):
                events.append('checkpoint')
                return original_mark(*args, **kw)
            def barrier(*args, **kw):
                events.append('barrier')
                return original_barrier(*args, **kw)
            async def verify(*args, **kw):
                events.append('verify-arm' if kw.get('arm') else 'verify-ready')
                return True, ''
            async def press(key):
                if key == 'Enter': events.append('enter')
            chat.press.side_effect = press
            with patch.object(friend_chat_guard, 'verify_friend', side_effect=verify), \
                 patch.object(friend_chat_guard, 'dispatch_was_allowed', AsyncMock(return_value=True)), \
                 patch.object(tasks, '_mark_browser_target_in_flight', side_effect=mark), \
                 patch.object(tasks, '_persist_browser_pre_enter_unknown', side_effect=barrier):
                asyncio.run(run())
            self.assertEqual(events[:5], ['verify-ready','checkpoint','barrier','verify-arm','enter'])

    def test_binding_change_after_barrier_prevents_enter(self):
        from copy import deepcopy
        with flow(identity_guard=True) as (user, page, context, browser, chat, release, run):
            user['targets'] = ['first']
            user['target_uid_bindings'] = {'first': {'uid': 'old-identity'}}
            disk = deepcopy(user)
            original = tasks._persist_browser_pre_enter_unknown
            def update(fn, **kw):
                result, changed = fn([disk])
                return deepcopy(result)
            def barrier(*args, **kw):
                result = original(*args, **kw)
                disk['target_uid_bindings']['first']['uid'] = 'new-identity'
                return result
            async def verify(page, account, target, **kw):
                return account['target_uid_bindings']['first']['uid'] == 'old-identity', 'binding changed'
            with patch.object(tasks, 'get_userData', side_effect=lambda **kw: [deepcopy(disk)]), \
                 patch.object(tasks, 'update_user_data', side_effect=update), \
                 patch.object(tasks, '_persist_browser_pre_enter_unknown', side_effect=barrier), \
                 patch.object(friend_chat_guard, 'verify_friend', side_effect=verify):
                asyncio.run(run())
            chat.press.assert_not_awaited()

    def test_final_identity_failure_keeps_unknown_and_does_not_continue(self):
        from datetime import datetime, timedelta, timezone
        from core import streak_state
        fixed_now = datetime(2026, 11, 7, 4, 0, tzinfo=timezone.utc)
        with flow(identity_guard=True) as (user, page, context, browser, chat, release, run), \
                patch.object(tasks, 'datetime', wraps=datetime) as clock:
            # This tests same-day unknown protection, not midnight rollover.
            # Freeze the writer and the assertion to the same business instant.
            clock.now.side_effect = lambda tz=None: (
                fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)
            )
            async def verify(*args, **kw):
                return (False, 'changed at final check') if kw.get('arm') else (True, '')
            with patch.object(friend_chat_guard, 'verify_friend', side_effect=verify), \
                 patch.object(tasks, 'detect_message_already_sent', AsyncMock()) as recovery:
                asyncio.run(run())
            chat.press.assert_not_awaited()
            recovery.assert_not_awaited()
            now = fixed_now + timedelta(minutes=45)
            self.assertTrue(streak_state.is_sent_unverified(user, 'first', now))
            due, *_=tasks._select_due_targets(user, {'enabled':True,'startHour':0,'endHour':24}, now)
            self.assertNotIn('first', due)

    def test_unready_friend_aborts_before_checkpoint_or_enter(self):
        with flow(identity_guard=True) as (user, page, context, browser, chat, release, run):
            with patch.object(friend_chat_guard, 'verify_friend', AsyncMock(return_value=(False,'previous chat still open'))), \
                 patch.object(tasks, '_mark_browser_target_in_flight') as mark, \
                 patch.object(tasks, 'detect_message_already_sent', AsyncMock()) as recovery:
                asyncio.run(run())
            mark.assert_not_called()
            recovery.assert_not_awaited()
            chat.press.assert_not_awaited()
            self.assertEqual(user['account_failure']['category'], 'friend_identity_unavailable')
