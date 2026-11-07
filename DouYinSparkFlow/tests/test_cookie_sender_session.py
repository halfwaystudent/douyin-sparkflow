"""Sender uses generation-aware persistent cookie sync without live traffic."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core import tasks
from tests.test_browser_stage_timeouts import flow, hangs

PROFILE = {'enabled': True, 'root': 'synthetic-profile', 'syncStoredCookiesBeforeRun': True,
           'seedCookiesWhenEmpty': True, 'refreshStoredCookiesAfterLogin': True}
STRATEGY = {'shuffleTargets': False, 'accountStartDelaySecondsMin': 0, 'accountStartDelaySecondsMax': 0,
            'messageIntervalSecondsMin': 0, 'messageIntervalSecondsMax': 0}


class CookieSenderSessionTests(unittest.TestCase):
    def run_flow(self, user, context, *, prepare, publish, login_error=None, message_hang=False):
        async def run():
            return await tasks.do_user_task(None, user, asyncio.Semaphore(1), STRATEGY, PROFILE,
                                            {'maxScanSeconds': 1}, 'direct', run_id='cookie-sync-test')
        driver = Mock(stop=AsyncMock())
        with patch.object(tasks, 'get_persistent_browser_context', AsyncMock(return_value=(driver, context, 'synthetic-profile'))), \
             patch.object(tasks, 'prepare_profile_cookies', prepare, create=True), \
             patch.object(tasks, 'publish_profile_cookies', publish, create=True):
            if login_error:
                with patch.object(tasks, 'ensure_not_login_required', AsyncMock(side_effect=login_error)):
                    asyncio.run(run())
            elif message_hang:
                with patch.object(tasks, 'confirm_message_sent', AsyncMock(side_effect=hangs)), \
                     patch.object(tasks, 'BROWSER_STAGE_TIMEOUT_SECONDS', .01):
                    asyncio.run(run())
            else:
                asyncio.run(run())

    def test_sender_prepares_once_and_exports_login_and_final_rotations(self):
        with flow() as (user, page, context, browser, chat, release, run):
            user['login_generation'] = 'synthetic-generation'
            session = SimpleNamespace(account=deepcopy(user))
            prepare = AsyncMock(return_value=session); publish = AsyncMock(return_value=True)
            self.run_flow(user, context, prepare=prepare, publish=publish)
            prepare.assert_awaited_once()
            self.assertEqual(publish.await_count, 2)
            release.assert_called_once()

    def test_failed_login_does_not_export_profile_over_saved_login(self):
        with flow() as (user, page, context, browser, chat, release, run):
            session = SimpleNamespace(account=deepcopy(user))
            prepare = AsyncMock(return_value=session); publish = AsyncMock(return_value=True)
            self.run_flow(user, context, prepare=prepare, publish=publish, login_error=RuntimeError('login_required'))
            prepare.assert_awaited_once()
            publish.assert_not_awaited()
            chat.press.assert_not_awaited()
            context.close.assert_awaited_once()

    def test_cancelled_final_snapshot_still_closes_context_and_releases_lock(self):
        with flow() as (user, page, context, browser, chat, release, run):
            session = SimpleNamespace(account=deepcopy(user))
            prepare = AsyncMock(return_value=session)
            publish = AsyncMock(side_effect=[True, asyncio.CancelledError()])
            with self.assertRaises(asyncio.CancelledError):
                self.run_flow(user, context, prepare=prepare, publish=publish)
            context.close.assert_awaited_once()
            release.assert_called_once()

    def test_timed_out_final_snapshot_still_closes_and_preserves_delivery(self):
        with flow() as (user, page, context, browser, chat, release, run):
            session = SimpleNamespace(account=deepcopy(user))
            prepare = AsyncMock(return_value=session)
            calls = 0
            async def export(*args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    await hangs()
                return True
            publish = AsyncMock(side_effect=export)
            with patch.object(tasks, 'BROWSER_STAGE_TIMEOUT_SECONDS', .015):
                self.run_flow(user, context, prepare=prepare, publish=publish)
            self.assertTrue(tasks.streak_state.is_sent_unverified(
                user, 'first', tasks.datetime.now(tasks._schedule_timezone())))
            context.close.assert_awaited_once()
            release.assert_called_once()

    def test_new_login_saved_during_typing_blocks_enter_for_friends_and_groups(self):
        for targets in [['first', 'second', 'third'], ['群聊：synthetic-group']]:
            with self.subTest(targets=targets), flow() as (user, page, context, browser, chat, release, run):
                user.update(login_generation='synthetic-old', targets=targets)
                disk = deepcopy(user)
                session = SimpleNamespace(account=deepcopy(user))
                def update(fn, **kwargs):
                    result, changed = fn([disk]); return result
                async def type_message(*args, **kwargs):
                    disk['login_generation'] = 'synthetic-new'
                    disk['cookies'] = [{'name': 'sessionid', 'value': 'synthetic-new-cookie', 'domain': '.douyin.com', 'path': '/'}]
                chat.type.side_effect = type_message
                with patch.object(tasks, 'update_user_data', side_effect=update), \
                     patch.object(tasks, 'get_userData', side_effect=lambda **kw: [deepcopy(disk)]):
                    self.run_flow(user, context, prepare=AsyncMock(return_value=session), publish=AsyncMock(return_value=True))
                chat.press.assert_not_awaited()
                context.close.assert_awaited_once()
                self.assertEqual('synthetic-new', disk['login_generation'])
                self.assertEqual('synthetic-new-cookie', disk['cookies'][0]['value'])

    def test_login_change_after_arm_or_queued_dispatch_still_blocks_enter(self):
        for moment in ['arm', 'dispatch_queue']:
            with self.subTest(moment=moment), flow() as (user, page, context, browser, chat, release, run):
                user['login_generation'] = 'synthetic-old'
                disk = deepcopy(user); session = SimpleNamespace(account=deepcopy(user))
                original_stage = tasks._run_browser_stage
                def update(fn, **kwargs):
                    result, changed = fn([disk]); return result
                async def guard(*args, **kwargs):
                    if moment == 'arm' and kwargs.get('arm'):
                        disk['login_generation'] = 'synthetic-new'
                async def stage(name, operation, **kwargs):
                    if moment == 'dispatch_queue' and name == 'dispatch_enter':
                        disk['login_generation'] = 'synthetic-new'
                    return await original_stage(name, operation, **kwargs)
                with patch.object(tasks, 'update_user_data', side_effect=update), \
                     patch.object(tasks, 'get_userData', side_effect=lambda **kw: [deepcopy(disk)]), \
                     patch.object(tasks, '_require_friend_chat', side_effect=guard), \
                     patch.object(tasks, '_run_browser_stage', side_effect=stage):
                    self.run_flow(user, context, prepare=AsyncMock(return_value=session), publish=AsyncMock(return_value=True))
                chat.press.assert_not_awaited()
                self.assertTrue(tasks.streak_state.is_sent_unverified(
                    disk, 'first', tasks.datetime.now(tasks._schedule_timezone())), 'durable unknown barrier survives')
                context.close.assert_awaited_once()

    def test_new_login_during_old_session_blocks_dispatch_even_for_manual_retry(self):
        for manual in [False, True]:
            user = {'username': 'synthetic', 'unique_id': 'synthetic-cookie', 'targets': ['friend'],
                    'login_generation': 'old-synthetic-generation'}
            disk = deepcopy(user); disk['login_generation'] = 'new-synthetic-generation'
            disk['cookies'] = [{'name': 'sessionid', 'value': 'new-synthetic-cookie', 'domain': '.douyin.com', 'path': '/'}]
            before = deepcopy(disk)
            def update(fn, **kwargs):
                result, changed = fn([disk]); return result
            with patch.object(tasks, 'update_user_data', side_effect=update), \
                 patch.object(tasks, 'get_userData', side_effect=lambda **kw: [deepcopy(disk)]):
                self.assertFalse(tasks._mark_browser_target_in_flight(user, 'friend', run_id='old-session', allow_retry=manual))
            self.assertEqual(before, disk, 'old session must not write into a new saved login')

    def test_wedged_page_does_not_run_final_cookie_read(self):
        with flow() as (user, page, context, browser, chat, release, run):
            session = SimpleNamespace(account=deepcopy(user))
            prepare = AsyncMock(return_value=session); publish = AsyncMock(return_value=True)
            self.run_flow(user, context, prepare=prepare, publish=publish, message_hang=True)
            self.assertEqual(publish.await_count, 1, 'login snapshot only; do not probe a wedged context')


if __name__ == '__main__':
    unittest.main()
