"""Minute-precision windows retain stable deterministic target scheduling."""
from datetime import datetime,timezone
import unittest

from core import tasks


class MinuteScheduleWindowTests(unittest.TestCase):
    def test_same_hour_minute_window_can_schedule_without_dividing_by_zero(self):
        window=tasks._normalize_send_window({'dailySendWindow':{'enabled':True,'startHour':8,'endTime':'08:30'}})
        now=datetime(2026,11,3,8,1,tzinfo=timezone.utc)
        account={'unique_id':'123','username':'offline'}
        scheduled=tasks._scheduled_send_time(account,'offline-friend',window,now)
        self.assertGreaterEqual(scheduled,now.replace(hour=8,minute=0,second=0,microsecond=0))
        self.assertLess(scheduled,now.replace(hour=8,minute=30,second=0,microsecond=0))
        self.assertEqual(scheduled,tasks._scheduled_send_time(account,'offline-friend',window,now))

    def test_exact_personal_start_time_remains_exact(self):
        window=tasks._normalize_send_window({'dailySendWindow':{'enabled':True,'startHour':8,'endTime':'08:30','exactTime':'08:17'}})
        now=datetime(2026,11,3,8,20,tzinfo=timezone.utc)
        self.assertEqual(now.replace(minute=17,second=0,microsecond=0),tasks._scheduled_send_time({'unique_id':'123'},'offline',window,now))
