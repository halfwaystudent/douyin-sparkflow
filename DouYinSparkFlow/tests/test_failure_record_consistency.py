"""Synthetic regression fixtures; never contacts Douyin."""
import unittest
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from core import streak_state, tasks, refusal_retry
from tests.test_refusal_retry import NOW, WINDOW, account


class FailureConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'SPARKFLOW_MANUAL_RUN': '', 'SPARKFLOW_FALLBACK_PHASE': '', 'SPARKFLOW_8610_RETRY_ONLY': '', 'SPARKFLOW_TIMEZONE': 'Asia/Shanghai'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def bound_account(self):
        a = account()
        fresh = deepcopy(a['failure_queue']['friend'])
        old = deepcopy(fresh)
        old['lastAttemptAt'] = (NOW-timedelta(days=5)).isoformat(timespec='seconds')
        a['target_refs']['friend'] = 'sec:demo-stable'
        a['target_states']['sec:demo-stable'] = a['target_states'].pop('nickname:friend')
        a['target_states']['sec:demo-stable']['targetRef'] = 'sec:demo-stable'
        a['failure_queue'] = {'sec:demo-stable': old, 'friend': fresh}
        return a, fresh

    def test_navigation_pause_preserves_target_rejection_receipt(self):
        a, fresh = self.bound_account()
        disk = deepcopy(a)
        def update(fn, **kw):
            result, changed = fn([disk])
            return deepcopy(result)
        with patch.object(tasks, 'update_user_data', side_effect=update):
            tasks._persist_account_send_failure(a, 'navigation_timeout', 'navigation failed', NOW.isoformat(), ['friend'])
        for saved in [disk, a]:
            self.assertEqual(streak_state.failure_entry(saved, 'friend', NOW.tzinfo)['lastAttemptAt'], fresh['lastAttemptAt'])
            self.assertTrue(refusal_retry.eligible(saved, 'friend', NOW))

    def test_success_clears_stable_failure_but_keeps_quota_and_other_target(self):
        a, _ = self.bound_account()
        a['failure_queue']['other'] = {'category': 'timeout', 'lastAttemptAt': NOW.isoformat()}
        refusal_retry.reserve(a, 'friend', NOW, 'retry-demo')
        disk = deepcopy(a)
        def update(fn, **kw):
            result, changed = fn([disk])
            return deepcopy(result)
        receipt = {'call': 'message_send', 'ok': True, 'httpStatus': 200, 'protoOk': True}
        with patch.object(tasks, 'update_user_data', side_effect=update):
            tasks._persist_browser_send_success(a, 'friend', 'demo message', NOW.isoformat(), receipt)
        for saved in [disk, a]:
            self.assertFalse(streak_state.failure_entry(saved, 'friend', NOW.tzinfo))
            self.assertIn('other', saved['failure_queue'])
            self.assertTrue(refusal_retry.used_today(saved, 'friend', NOW))
            self.assertIsNone(refusal_retry.reserve(saved, 'friend', NOW, 'second-retry'))

    def test_real_file_navigation_failure_preserves_rejection_after_reload(self):
        import json
        import tempfile
        from pathlib import Path
        from utils.config import update_user_data
        a, fresh = self.bound_account()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'usersData.json'
            path.write_text(json.dumps([a]))
            def update(fn, **kw):
                return update_user_data(fn, path=path)
            with patch.object(tasks, 'update_user_data', side_effect=update):
                tasks._persist_account_send_failure(a, 'navigation_timeout', 'demo timeout', NOW.isoformat(), ['friend'])
            disk = json.loads(path.read_text())[0]
            self.assertEqual(streak_state.failure_entry(disk, 'friend', NOW.tzinfo)['lastAttemptAt'], fresh['lastAttemptAt'])
            self.assertTrue(refusal_retry.eligible(disk, 'friend', NOW))

    def test_two_names_of_same_identity_are_cleared_together(self):
        a, _ = self.bound_account()
        a['target_refs']['renamed'] = 'sec:demo-stable'
        a['failure_queue']['renamed'] = deepcopy(a['failure_queue']['friend'])
        streak_state.clear_failure_entries(a, 'renamed')
        self.assertFalse(a.get('failure_queue'))

    def test_stale_queue_cannot_schedule_terminal_state_as_normal_pending(self):
        a, _ = self.bound_account()
        a['failure_queue'].pop('friend')
        with patch.object(tasks, '_scheduled_send_time', return_value=NOW-timedelta(hours=1)):
            self.assertEqual(tasks._select_due_targets(a, WINDOW, NOW)[0], [])

    def test_success_history_wins_over_stale_failed_state(self):
        from webui.ops import _build_target_status
        a, _ = self.bound_account()
        a['failure_queue'].pop('friend')
        a['message_history'] = {'sec:demo-stable': {
            'sentAt': NOW.isoformat(), 'status': 'confirmed',
            'confirmationLevel': 'strong', 'confirmationSource': 'cdp_message_send_receipt',
        }}
        self.assertEqual(_build_target_status(a, 'friend', NOW, WINDOW)['status'], 'sent')

    def test_cleanup_does_not_delete_reused_old_nickname(self):
        a, _ = self.bound_account()
        a['target_refs']['renamed'] = 'sec:demo-stable'
        a['target_refs']['friend'] = 'sec:new-person'
        a['failure_queue']['friend'] = {'category': 'timeout', 'lastAttemptAt': NOW.isoformat()}
        streak_state.clear_failure_entries(a, 'renamed')
        self.assertIn('friend', a.get('failure_queue', {}))
        self.assertNotIn('sec:demo-stable', a.get('failure_queue', {}))

    def test_previous_day_terminal_does_not_block_new_day(self):
        a, _ = self.bound_account()
        a['failure_queue'].pop('friend')
        tomorrow = NOW + timedelta(days=1)
        with patch.object(tasks, '_scheduled_send_time', return_value=tomorrow-timedelta(hours=1)):
            self.assertEqual(tasks._select_due_targets(a, WINDOW, tomorrow)[0], ['friend'])

    def test_cleanup_block_survives_late_failure_success_and_reload(self):
        import json
        import tempfile
        from pathlib import Path
        from utils.config import update_user_data
        a, _ = self.bound_account()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'usersData.json'
            path.write_text(json.dumps([a]))
            with patch.object(tasks, 'update_user_data', side_effect=lambda fn, **kw: update_user_data(fn, path=path)):
                tasks._persist_account_send_failure(a, 'browser_cleanup_blocked', 'cleanup uncertain', NOW.isoformat(), ['friend'])
                tasks._persist_account_send_failure(a, 'browser_stage_timeout', 'late stage', NOW.isoformat(), ['friend'])
                tasks._persist_browser_send_success(a, 'friend', 'demo', NOW.isoformat(),
                    {'ok': True, 'call': 'message_send', 'httpStatus': 200, 'protoOk': True})
            saved = json.loads(path.read_text())[0]
            self.assertTrue(saved.get('browser_cleanup_blocked'))
            self.assertTrue(tasks._account_paused_by_failure_today(saved, NOW+timedelta(days=1)))

    def test_console_reports_state_failure_when_queue_is_stale(self):
        from webui.ops import _build_target_status
        a, _ = self.bound_account()
        a['failure_queue'].pop('friend')
        row = _build_target_status(a, 'friend', NOW, WINDOW)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['category'], 'send_rejected')
        self.assertIn('补试受阻', row['reason'])


if __name__ == '__main__':
    unittest.main()
