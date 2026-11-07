"""Network-free caller regressions for the opt-in daily recovery engine."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import json
from contextlib import ExitStack, contextmanager
import unittest
from unittest.mock import AsyncMock, patch
from core import tasks, recovery_queue

CFG = {'automaticRecovery': {'enabled': True},
       'dailySendWindow': {'enabled': True, 'startHour': 10, 'endHour': 17, 'scheduleIntervalMinutes': 20},
       'useProtocolSender': False, 'multiTask': False, 'taskCount': 1}
NOW = datetime(2026, 11, 6, 18, 0, tzinfo=timezone(timedelta(hours=8)))


class DispatchProtectionTests(unittest.TestCase):
    def test_manual_quiet_hours_refuse_browser_before_launch(self):
        with patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)), \
                patch.object(tasks, 'datetime') as clock, \
                patch.object(tasks, '_run_browser_tasks', new_callable=AsyncMock) as sender, \
                patch.object(tasks.send_pause, 'should_pause_sending', return_value=False):
            clock.now.return_value = NOW.replace(hour=9, minute=59)
            asyncio.run(tasks.run_browser_tasks(CFG, [{'_personal_scope': {}}]))
            sender.assert_not_awaited()

    def test_direct_manual_protocol_claim_observes_quiet_hours_before_writing(self):
        from core import protocol_dispatch
        with patch.dict(os.environ, {'SPARKFLOW_MANUAL_RUN': '1', 'SPARKFLOW_RECOVERY_PASS': '',
                                    'SPARKFLOW_RECONCILE_ONLY': '', 'SPARKFLOW_PERSONAL_USER_ID': ''}), \
                patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)), \
                patch.object(protocol_dispatch, 'datetime') as clock, \
                patch.object(protocol_dispatch, 'update_user_data', return_value=None) as update:
            clock.now.return_value = NOW.replace(hour=9, minute=59)
            self.assertEqual(protocol_dispatch._mark_protocol_targets_in_flight({}, 'quiet', allow_retry=True), [])
            update.assert_not_called()

    def test_feature_off_does_not_add_a_protocol_cutoff(self):
        from types import SimpleNamespace
        from core import protocol_dispatch
        process = SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'sent': []}), stderr='')
        with patch.object(recovery_queue, 'get_config', return_value={}), \
                patch.object(protocol_dispatch.send_pause, 'should_pause_sending', return_value=False), \
                patch.object(protocol_dispatch, '_build_protocol_command',
                    return_value=(['node', 'synthetic'], Path.cwd(), 'synthetic', '/synthetic', None)), \
                patch.object(protocol_dispatch.subprocess, 'run', return_value=process) as launch:
            protocol_dispatch._run_protocol_for_user({}, {}, False, {})
            payload = json.loads(launch.call_args.kwargs['input'])
            self.assertNotIn('personalSendDeadlineEpochMs', payload)

    def test_recovery_origin_cannot_skip_protocol_guard_by_omitting_run_id(self):
        from core import protocol_dispatch
        with patch.dict(os.environ, {'SPARKFLOW_PERSONAL_USER_ID': '', 'SPARKFLOW_RECOVERY_PASS': '1'}), \
                patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)), \
                patch.object(protocol_dispatch, '_build_protocol_command',
                    side_effect=AssertionError('recovery must not launch protocol')):
            result = protocol_dispatch._run_protocol_for_user({}, {}, False, {})
            self.assertEqual(result['runner'], 'recovery-browser-only')

    def test_recovery_policy_cannot_be_bypassed_by_direct_protocol_claim(self):
        from core import protocol_dispatch
        from tests.test_refusal_retry import account
        user = account()
        def update(fn, **_kwargs):
            result, _changed = fn([user])
            return result
        with patch.dict(os.environ, {'SPARKFLOW_RECOVERY_PASS': '1', 'SPARKFLOW_PERSONAL_USER_ID': ''}), \
                patch('utils.config.get_config', return_value=deepcopy(CFG)), \
                patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)), \
                patch.object(protocol_dispatch, 'update_user_data', side_effect=update), \
                patch.object(protocol_dispatch, 'get_userData', return_value=[user]):
            self.assertEqual(protocol_dispatch._mark_protocol_targets_in_flight(user, 'bypass', allow_retry=True), [])

    def test_even_a_queued_enter_cannot_cross_midnight_morning_or_2350(self):
        for hour, minute in [(0, 0), (9, 59), (23, 50), (23, 59)]:
            with self.subTest(hour=hour, minute=minute), \
                    patch.object(tasks, 'get_config', return_value=deepcopy(CFG)), \
                    patch('utils.config.get_config', return_value=deepcopy(CFG)), \
                patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)), \
                    patch.object(tasks, 'datetime') as clock, \
                    patch.object(tasks.personal_streak, 'authorized', return_value=True), \
                    patch.object(tasks.personal_streak, 'within_send_window', return_value=True), \
                    patch.object(tasks, '_browser_login_generation_current', return_value=True), \
                    patch.object(tasks.send_pause, 'should_pause_sending', return_value=False):
                clock.now.return_value = NOW.replace(hour=hour, minute=minute)
                chat = type('Chat', (), {'press': AsyncMock()})()
                self.assertFalse(asyncio.run(tasks._dispatch_browser_enter_if_current({}, chat)))
                chat.press.assert_not_awaited()

    def test_manual_route_keeps_quiet_hours_and_allows_healthy_daytime(self):
        with patch.object(tasks, 'get_config', return_value=deepcopy(CFG)), \
                patch('utils.config.get_config', return_value=deepcopy(CFG)), \
                patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)), \
                patch.object(tasks, 'datetime') as clock, \
                patch.object(tasks.personal_streak, 'authorized', return_value=True), \
                patch.object(tasks.personal_streak, 'within_send_window', return_value=True), \
                patch.object(tasks, '_browser_login_generation_current', return_value=True), \
                patch.object(tasks.send_pause, 'should_pause_sending', return_value=False):
            clock.now.return_value = NOW
            chat = type('Chat', (), {'press': AsyncMock()})()
            self.assertTrue(asyncio.run(tasks._dispatch_browser_enter_if_current({}, chat)))
            chat.press.assert_awaited_once_with('Enter')


class RecoverySummaryTests(unittest.TestCase):
    def test_normal_window_end_is_not_the_final_failure_summary(self):
        self._check_summary(NOW.replace(hour=17), True, False)

    def test_2350_reconciliation_is_final_even_without_old_fallback_flag(self):
        self._check_summary(NOW.replace(hour=23, minute=50), False, True)

    def _check_summary(self, now, fallback, expected):
        from core import notifications
        with tempfile.TemporaryDirectory() as d, \
                patch.object(notifications.notify, 'enabled_channel_names', return_value=['synthetic']), \
                patch.object(notifications.notify, 'send_message'), \
                patch.object(notifications, '_target_rows', return_value=[{'confirmed': False}]), \
                patch.object(notifications, '_failure_entries', return_value=[]), \
                patch.object(notifications, '_summary_message', return_value={'synthetic': True}), \
                patch.object(notifications, '_identity_event_entries', return_value=[]):
            result = notifications.notify_run_finished('synthetic', 'reconciled', CFG,
                [{'enabled': True}], schedule_tz=NOW.tzinfo, send_window=CFG['dailySendWindow'],
                fallback_run=fallback, now=now, state_path=Path(d)/'notifications.json')
            self.assertEqual(result['summary'], expected)


@contextmanager
def recovery_store(accounts, now=NOW):
    """Exercise the real file transaction and an independent disk readback."""
    from utils import config as storage
    with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
        path = Path(d)/'accounts.json'
        path.write_text(json.dumps(accounts))
        stack.enter_context(patch.dict(os.environ, {
            'SPARKFLOW_TIMEZONE': 'Asia/Shanghai', 'SPARKFLOW_MANUAL_RUN': '',
            'SPARKFLOW_FALLBACK_PHASE': '', 'SPARKFLOW_8610_RETRY_ONLY': '',
            'SPARKFLOW_RECOVERY_PASS': '1', 'SPARKFLOW_PERSONAL_USER_ID': '',
        }))
        stack.enter_context(patch.object(storage, 'get_config', return_value=deepcopy(CFG)))
        stack.enter_context(patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)))
        stack.enter_context(patch.object(tasks, 'get_config', return_value=deepcopy(CFG)))
        stack.enter_context(patch.object(tasks, 'get_userData', side_effect=lambda **kw: json.loads(path.read_text())))
        stack.enter_context(patch.object(tasks, 'update_user_data',
            side_effect=lambda fn, **kw: storage.update_user_data(fn, path=path)))
        clock = stack.enter_context(patch.object(tasks, 'datetime'))
        clock.now.return_value = now
        yield path, clock


class RecoveryCallerTests(unittest.TestCase):
    @staticmethod
    def fresh():
        return {'account_ref': 'synthetic-account', 'login_generation': 'synthetic-generation',
                'username': 'synthetic', 'unique_id': 'synthetic', 'targets': ['friend'],
                'enabled': True, 'cookies': [{'name': 'sessionid', 'value': 'synthetic', 'expires': -1}]}

    def test_real_transaction_claim_is_single_use_and_reads_back(self):
        from core import automatic_recovery as A, recovery_queue as Q, streak_state
        now = NOW.replace(hour=16)
        user = self.fresh()
        with recovery_store([user], now) as (path, _clock), \
                patch.object(tasks, '_scheduled_send_time', return_value=now-timedelta(minutes=1)):
            scoped = deepcopy(user)
            scoped[A.TICKET] = {'day': now.date().isoformat(), 'target': 'friend', 'runId': 'one'}
            self.assertEqual(tasks._browser_prelaunch_targets(user, ['friend'], False, scope_user=scoped), ['friend'])
            self.assertTrue(tasks._mark_browser_target_in_flight(scoped, 'friend', run_id='one'))
            on_disk = json.loads(path.read_text())[0]
            self.assertEqual(streak_state.target_state(on_disk, 'friend', now)['runId'], 'one')
            self.assertTrue(Q.reservation_matches(on_disk, 'friend', now, 'one', scoped[A.TICKET]['reservation']))
            self.assertFalse(tasks._mark_browser_target_in_flight(scoped, 'friend', run_id='one'))
            # An unknown-outcome barrier is expected at Enter and does not
            # invalidate this same proven reservation.
            self.assertTrue(tasks._persist_browser_pre_enter_unknown(scoped, 'friend', 'synthetic', now.isoformat()))
            self.assertTrue(A.dispatch_allowed(scoped, now))

    def test_login_flag_changed_after_planning_refuses_browser_prelaunch(self):
        from core import automatic_recovery as A
        now = NOW.replace(hour=16)
        user = self.fresh()
        user['login_required'] = True
        with recovery_store([user], now), \
                patch.object(tasks, '_scheduled_send_time', return_value=now-timedelta(minutes=1)):
            scoped = deepcopy(user)
            scoped[A.TICKET] = {'day': now.date().isoformat(), 'target': 'friend', 'runId': 'blocked'}
            self.assertEqual(tasks._browser_prelaunch_targets(user, ['friend'], False, scope_user=scoped), [])

    def test_fresh_bad_login_identity_or_disabled_state_cannot_claim(self):
        from core import automatic_recovery as A
        now = NOW.replace(hour=16)
        for flag in ['login_required', 'needs_relogin', 'pending_login_verification',
                     'identity_mismatch', 'account_identity_mismatch', 'enabled']:
            with self.subTest(flag=flag):
                user = self.fresh()
                source = deepcopy(user)
                user[flag] = flag != 'enabled'
                source[A.TICKET] = {'day': now.date().isoformat(), 'target': 'friend', 'runId': 'blocked'}
                with recovery_store([user], now) as (path, _clock), \
                        patch.object(tasks, '_scheduled_send_time', return_value=now-timedelta(minutes=1)):
                    before = path.read_bytes()
                    self.assertFalse(tasks._mark_browser_target_in_flight(source, 'friend', run_id='blocked'))
                    self.assertEqual(path.read_bytes(), before)

    def test_real_8610_and_binary_8101_can_repeat_after_wait_without_a_daily_cap(self):
        from core import automatic_recovery as A, recovery_queue as Q
        from tests.test_refusal_retry import account as rejected
        from tests.test_refusal_8101_fallback import refused_8101
        receipts = [rejected()['failure_queue']['friend']['serverReceipt'],
                    refused_8101()['failure_queue']['friend']['serverReceipt']]
        user = self.fresh()
        start = NOW.replace(hour=15)
        with recovery_store([user], start) as (path, clock), \
                patch.object(Q.random, 'randint', return_value=45), \
                patch.object(tasks, '_scheduled_send_time', return_value=start-timedelta(minutes=1)):
            for number in range(7):
                now = start+timedelta(minutes=50*number)
                clock.now.return_value = now
                scoped = json.loads(path.read_text())[0]
                scoped['targets'] = ['friend']
                scoped[A.TICKET] = {'day': now.date().isoformat(), 'target': 'friend', 'runId': str(number)}
                self.assertTrue(tasks._mark_browser_target_in_flight(scoped, 'friend', run_id=str(number)))
                self.assertTrue(tasks._persist_browser_pre_enter_unknown(scoped, 'friend', 'synthetic', now.isoformat()))
                tasks._persist_browser_send_failure(scoped, 'friend', 'synthetic', 'send_rejected',
                    'synthetic', (now+timedelta(seconds=1)).astimezone(timezone.utc).isoformat(timespec='seconds'), server_receipt=receipts[number % 2],
                    override_sent_unverified=True)
                saved = json.loads(path.read_text())[0]
                row = Q.target_entry(saved, 'friend', now+timedelta(seconds=1))
                self.assertEqual(row['sequence'], number+1)
                self.assertFalse(Q.due(saved, 'friend', now+timedelta(minutes=44)))
                self.assertTrue(Q.due(saved, 'friend', now+timedelta(minutes=46)))

    def test_lost_readback_never_authorizes_enter_and_keeps_reservation(self):
        from core import automatic_recovery as A, recovery_queue as Q
        now = NOW.replace(hour=16)
        user = self.fresh()
        with recovery_store([user], now) as (path, _clock), \
                patch.object(tasks, '_scheduled_send_time', return_value=now-timedelta(minutes=1)), \
                patch.object(tasks, 'get_userData', return_value=[]):
            user[A.TICKET] = {'day': now.date().isoformat(), 'target': 'friend', 'runId': 'lost'}
            self.assertFalse(tasks._mark_browser_target_in_flight(user, 'friend', run_id='lost'))
            self.assertEqual(Q.target_entry(json.loads(path.read_text())[0], 'friend', now)['state'], 'claimed')
            self.assertFalse(A.dispatch_allowed(user, now))

    def test_reconciliation_updates_only_local_queue_and_never_launches_browser(self):
        from core import automatic_recovery as A, recovery_queue as Q
        user = self.fresh()
        with recovery_store([user]) as (path, _clock), \
                patch.object(tasks.personal_streak, 'account_groups', side_effect=lambda c, a, **kw: [(c, a)]), \
                patch.object(tasks, 'run_browser_tasks', new_callable=AsyncMock) as sender, \
                patch.object(tasks, '_append_streak_run_report'), \
                patch.object(tasks.notifications, 'notify_run_finished'):
            self.assertEqual(asyncio.run(A.run(CFG, reconcile_only=True)), 'reconciled')
            sender.assert_not_awaited()
            row = Q.target_entry(json.loads(path.read_text())[0], 'friend', NOW)
            self.assertEqual(row['kind'], 'unsent')

    def test_global_run_selects_only_one_ordinary_target_before_refusal(self):
        from core import automatic_recovery as A, streak_state
        from tests.test_refusal_retry import account as rejected
        now = NOW.replace(hour=16)
        user = self.fresh()
        user['targets'] = ['friend', 'ordinary', '群聊:7691234567890123401']
        refused_at = now-timedelta(minutes=100)
        streak_state.mark_failed(user, 'friend', category='send_rejected', reason='synthetic', retryable=False, now=refused_at)
        user['failure_queue'] = deepcopy(rejected()['failure_queue'])
        user['failure_queue']['friend']['lastAttemptAt'] = refused_at.isoformat()
        with recovery_store([user], now), \
                patch.object(tasks.personal_streak, 'account_groups', side_effect=lambda c, a, **kw: [(c, a)]), \
                patch.object(tasks, '_scheduled_send_time', return_value=now-timedelta(minutes=1)), \
                patch.object(tasks, 'run_browser_tasks', new_callable=AsyncMock) as sender, \
                patch.object(tasks, '_append_streak_run_report'), \
                patch.object(tasks.notifications, 'notify_run_finished'):
            self.assertEqual(asyncio.run(A.run(CFG)), 'completed')
            sender.assert_awaited_once()
            selected = sender.await_args.args[1]
            self.assertEqual(len(selected), 1)
            self.assertEqual(len(selected[0]['targets']), 1)
            self.assertNotEqual(selected[0]['targets'], ['friend'])
            self.assertFalse(sender.await_args.args[0]['multiTask'])

    def test_initial_page_failure_closes_before_random_account_cooldown_without_claim(self):
        from core import page_readiness, recovery_queue as Q
        from tests.test_browser_stage_timeouts import flow
        with flow() as fixture:
            user, _page, context, _browser, chat, release, run = fixture
            with recovery_store([user]) as (path, _clock), \
                    patch.object(Q.random, 'randint', return_value=45), \
                    patch.object(page_readiness, 'ensure_chat_page_ready',
                        new=AsyncMock(side_effect=tasks.BrowserStageTimeout('page_readiness'))), \
                    patch.object(tasks, '_mark_browser_target_in_flight') as claim, \
                    patch.object(tasks, '_persist_browser_send_failure') as target_failure:
                asyncio.run(run())
                chat.press.assert_not_awaited()
                claim.assert_not_called()
                target_failure.assert_not_called()
                context.close.assert_awaited_once()
                release.assert_called_once()
                tasks.ensure_not_login_required.assert_not_awaited()
                saved = json.loads(path.read_text())[0]
                self.assertTrue(Q.account_blocked(saved, NOW+timedelta(minutes=44)))
                self.assertFalse(tasks._account_paused_by_failure_today(saved, NOW+timedelta(minutes=46)))
                self.assertNotIn(Q.KEY, saved)
