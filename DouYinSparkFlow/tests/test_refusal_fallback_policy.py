"""8610 automatic retries belong to final fallback, after normal targets."""
import asyncio
import os
import unittest
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from core import tasks, streak_state
from tests.test_refusal_retry import account, NOW, WINDOW


class RefusalFallbackPolicyTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'SPARKFLOW_MANUAL_RUN': '', 'SPARKFLOW_FALLBACK_PHASE': '',
                                     'SPARKFLOW_8610_RETRY_ONLY': '', 'SPARKFLOW_TIMEZONE': 'Asia/Shanghai'})
        env.start(); self.addCleanup(env.stop)

    def test_cooled_rejection_waits_through_regular_and_closeout_runs(self):
        for at in [NOW, NOW.replace(minute=40)]:
            self.assertEqual(tasks._select_due_targets(account(), WINDOW, at)[0], [])

    def test_fallback_appends_rejections_after_normal_pending_targets(self):
        user = account(); user['targets'].extend(['new-a', 'new-b'])
        with patch.dict(os.environ, {'SPARKFLOW_MANUAL_RUN': '1', 'SPARKFLOW_FALLBACK_PHASE': '1'}):
            due = tasks._select_due_targets(user, WINDOW, NOW.replace(hour=17))[0]
        self.assertEqual(due, ['new-a', 'new-b', 'friend'])

    def test_old_extra_retry_mode_cannot_start_a_late_pass(self):
        with patch.dict(os.environ, {'SPARKFLOW_8610_RETRY_ONLY': '1'}):
            self.assertEqual(tasks._select_due_targets(account(), WINDOW, NOW.replace(hour=18))[0], [])

    def test_automatic_prelaunch_does_not_open_browser_for_regular_retry(self):
        with patch.object(tasks, 'datetime') as clock:
            clock.now.return_value = NOW
            self.assertEqual(tasks._browser_prelaunch_targets(account(), ['friend'], False), [])

    def test_automatic_dispatch_denies_regular_retry_without_consuming_quota(self):
        user = account()
        with patch.object(tasks, 'datetime') as clock, patch.object(tasks, 'update_user_data') as write:
            clock.now.return_value = NOW
            self.assertFalse(tasks._mark_browser_target_in_flight(user, 'friend', run_id='regular-denied'))
        write.assert_not_called()

    def test_fallback_still_enforces_cooldown_and_never_retries_unknown(self):
        end = NOW.replace(hour=17)
        user = account()
        late = end-timedelta(minutes=10)
        streak_state.mark_failed(user, 'friend', category='send_rejected', reason='synthetic', retryable=False, now=late)
        user['failure_queue']['friend']['lastAttemptAt'] = late.isoformat(timespec='seconds')
        user['targets'].append('unknown')
        streak_state.mark_sent_unverified(user, 'unknown', now=end-timedelta(minutes=60), detail='pre_enter_outcome_unknown_barrier')
        with patch.dict(os.environ, {'SPARKFLOW_MANUAL_RUN': '1', 'SPARKFLOW_FALLBACK_PHASE': '1'}):
            self.assertEqual(tasks._select_due_targets(user, WINDOW, end)[0], [])

    def test_final_fallback_accepts_twenty_minutes_but_not_one_second_less(self):
        end = NOW.replace(hour=17)
        for elapsed, expected in [(timedelta(minutes=20), ['friend']),
                                  (timedelta(minutes=20)-timedelta(seconds=1), [])]:
            user = account()
            refused_at = end-elapsed
            streak_state.mark_failed(user, 'friend', category='send_rejected', reason='synthetic', retryable=False, now=refused_at)
            user['failure_queue']['friend']['lastAttemptAt'] = refused_at.isoformat(timespec='seconds')
            with patch.dict(os.environ, {'SPARKFLOW_MANUAL_RUN': '1', 'SPARKFLOW_FALLBACK_PHASE': '1'}):
                self.assertEqual(tasks._select_due_targets(user, WINDOW, end)[0], expected)

    def test_protocol_configuration_routes_8610_to_reserved_browser_path(self):
        user = account(); user['targets'].append('new-friend')
        with patch.object(tasks, 'datetime') as clock:
            clock.now.return_value = NOW.replace(hour=17)
            protocol, browser = tasks._split_sender_modes({'useProtocolSender': True}, [user])
        self.assertEqual([u['targets'] for u in protocol], [['new-friend']])
        self.assertEqual([u['targets'] for u in browser], [['friend']])

    def test_direct_protocol_automatic_claim_cannot_bypass_browser_retry_quota(self):
        from core import protocol_dispatch
        for final in ['', '1']:
            disk = account()
            def update(fn, **kwargs):
                result, changed = fn([disk]); return result
            with patch.dict(os.environ, {'SPARKFLOW_FALLBACK_PHASE': final}), \
                 patch.object(protocol_dispatch, 'datetime') as clock, \
                 patch.object(protocol_dispatch, 'update_user_data', side_effect=update):
                clock.now.return_value = NOW.replace(hour=17)
                self.assertEqual(protocol_dispatch._mark_protocol_targets_in_flight(deepcopy(disk), 'no-bypass'), [])

    def test_manual_protocol_retry_consumes_quota_and_requires_readback(self):
        from core import protocol_dispatch, refusal_retry
        for readable in [True, False]:
            disk = account()
            def update(fn, **kwargs):
                result, changed = fn([disk]); return result
            with patch.object(protocol_dispatch, 'datetime') as clock, \
                 patch.object(protocol_dispatch, 'update_user_data', side_effect=update), \
                 patch.object(protocol_dispatch, 'get_userData', create=True,
                              side_effect=lambda **kw: [deepcopy(disk)] if readable else []):
                clock.now.return_value = NOW
                claimed = protocol_dispatch._mark_protocol_targets_in_flight(deepcopy(disk), 'manual-explicit', allow_retry=True)
            self.assertEqual(claimed, ['friend'] if readable else [])
            self.assertTrue(refusal_retry.used_today(disk, 'friend', NOW))

    def test_previous_day_rejection_is_a_new_daily_send_not_an_auto_retry(self):
        from core import protocol_dispatch, refusal_retry
        at = NOW+timedelta(days=1)
        user = account()
        with patch.object(tasks, 'datetime') as clock:
            clock.now.return_value = at
            protocol, browser = tasks._split_sender_modes({'useProtocolSender': True}, [user])
        self.assertEqual([u['targets'] for u in protocol], [['friend']])
        self.assertEqual(browser, [])
        for module, method in [(tasks, '_mark_browser_target_in_flight'),
                               (protocol_dispatch, '_mark_protocol_targets_in_flight')]:
            disk = account()
            def update(fn, **kwargs):
                result, changed = fn([disk]); return result
            with patch.object(module, 'datetime') as clock, \
                 patch.object(module, 'update_user_data', side_effect=update):
                clock.now.return_value = at
                if module is tasks:
                    self.assertTrue(getattr(module, method)(deepcopy(disk), 'friend', run_id='new-day'))
                else:
                    self.assertEqual(getattr(module, method)(deepcopy(disk), 'new-day'), ['friend'])
            self.assertFalse(refusal_retry.used_today(disk, 'friend', at))

    def test_previous_day_refusal_keeps_normal_fallback_priority(self):
        user = account(); user['targets'] = ['current-refusal', 'friend']
        at = NOW+timedelta(days=1)
        refused_at = at-timedelta(minutes=31)
        streak_state.mark_failed(user, 'current-refusal', category='send_rejected', reason='synthetic', retryable=False, now=refused_at)
        user['failure_queue']['current-refusal'] = deepcopy(user['failure_queue']['friend'])
        user['failure_queue']['current-refusal']['lastAttemptAt'] = refused_at.isoformat(timespec='seconds')
        async def scan(_page, _user, _name, targets, *_args, **_kwargs):
            for target in targets:
                yield target
        async def run():
            return [target async for target in tasks._scan_delivery_targets(None, user, 'synthetic', user['targets'], {})]
        with patch.dict(os.environ, {'SPARKFLOW_FALLBACK_PHASE': '1'}), \
             patch.object(tasks, 'datetime') as clock, \
             patch.object(tasks, 'scroll_and_select_user', side_effect=scan):
            clock.now.return_value = at
            self.assertEqual(asyncio.run(run()), ['friend', 'current-refusal'])

    def test_malformed_rejection_time_cannot_be_treated_as_a_new_day(self):
        from core import protocol_dispatch
        user = account()
        state_key = streak_state.resolve_target_ref(user, 'friend')
        user['target_states'][state_key]['lastAttemptAt'] = 'not-a-time'
        user['failure_queue']['friend']['lastAttemptAt'] = 'not-a-time'
        def update(fn, **kwargs):
            result, changed = fn([user]); return result
        with patch.object(protocol_dispatch, 'datetime') as clock, \
             patch.object(protocol_dispatch, 'update_user_data', side_effect=update):
            clock.now.return_value = NOW
            self.assertEqual(protocol_dispatch._mark_protocol_targets_in_flight(deepcopy(user), 'bad-clock'), [])

    def test_sender_scans_normal_batch_before_rejected_group_or_friend_batch(self):
        user = account()
        group = '群聊:7691234567890123401'
        user['targets'] = [group, 'new-friend', 'friend', '群聊:7691234567890123402']
        old = NOW-timedelta(minutes=31)
        streak_state.mark_failed(user, group, category='send_rejected', reason='synthetic', retryable=False, now=old)
        user['failure_queue'][group] = deepcopy(user['failure_queue']['friend'])
        seen = []
        async def scan(_page, _user, _name, targets, *_args, **kwargs):
            seen.append(list(targets))
            for target in targets:
                yield target
        async def run():
            result = []
            async for target in tasks._scan_delivery_targets(None, user, 'synthetic', user['targets'], {}):
                result.append(target)
            return result
        with patch.dict(os.environ, {'SPARKFLOW_FALLBACK_PHASE': '1'}), \
             patch.object(tasks, 'scroll_and_select_user', side_effect=scan), \
             patch.object(tasks, 'datetime') as clock:
            clock.now.return_value = NOW.replace(hour=17)
            result = asyncio.run(run())
        self.assertEqual(seen, [['new-friend', '群聊:7691234567890123402'], [group, 'friend']])
        self.assertEqual(result, ['new-friend', '群聊:7691234567890123402', group, 'friend'])


if __name__ == '__main__':
    unittest.main()
