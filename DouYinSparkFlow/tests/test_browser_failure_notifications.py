import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from core import notifications


class BrowserFailureNotificationTests(unittest.TestCase):
    def test_one_pre_send_failure_reports_one_batch_not_three_send_failures(self):
        now = datetime(2026, 10, 31, 11, 44, tzinfo=timezone(timedelta(hours=8)))
        a = {'username': 'demo', 'account_ref': 'demo-account', 'targets': ['one', 'two', 'three'],
             'account_failure': {'category': 'browser_stage_timeout', 'lastAttemptAt': now.isoformat(),
                                 'affectedTargets': ['one', 'two', 'three'], 'reason': 'login_check exceeded stage budget'}}
        rows = notifications._target_rows([deepcopy(a)], now, now.date(), now.tzinfo, final=False)
        entries = notifications._failure_entries([a], rows, now.date(), now.tzinfo)
        self.assertEqual(len(entries), 1)
        line = notifications._failure_line(entries[0])
        self.assertIn('3', line)
        self.assertIn('本轮', line)
        self.assertNotIn('预检失败', line)

    def test_console_matches_first_stage_failure_cooldown(self):
        from webui import ops
        now = datetime(2026, 10, 31, 11, 44, tzinfo=timezone(timedelta(hours=8)))
        failure = {'category': 'browser_stage_timeout', 'lastAttemptAt': now.isoformat(), 'attemptCount': 1}
        self.assertTrue(ops._account_failure_pause_active(failure, now, 2))
        self.assertFalse(ops._account_failure_pause_active(failure, now+timedelta(hours=2), 2))

    def test_past_day_stage_failure_does_not_notify_today(self):
        now = datetime(2026, 10, 31, 11, 44, tzinfo=timezone(timedelta(hours=8)))
        a = {'username': 'demo', 'targets': ['one'], 'account_failure': {
            'category': 'browser_stage_timeout', 'lastAttemptAt': (now-timedelta(days=1)).isoformat(), 'affectedTargets': ['one']}}
        rows = notifications._target_rows([deepcopy(a)], now, now.date(), now.tzinfo, final=False)
        self.assertEqual(notifications._failure_entries([a], rows, now.date(), now.tzinfo), [])
