"""Adding an end-time field must not silently reschedule existing plans."""
from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import patch
from core import personal_streak as streak
from webui import users
from tests import test_shared_security as fixtures


class LegacyPersonalPlanTests(unittest.TestCase):
    def test_unchanged_legacy_form_keeps_revision_and_due_timestamp(self):
        fixture=fixtures.SharedConsoleTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        principal={'username':'alice','role':'user'}
        values={'enabled':True,'at_time':'08:30','timezone_name':'Asia/Shanghai','messages':['existing']}
        now=datetime(2026,11,3,0,tzinfo=timezone.utc)
        with patch.object(streak,'_now',return_value=now):
            saved=streak.save_preferences(principal,**values)
        with users._web_users_lock():
            raw=users._load_raw()
            for profile in raw[streak.STORE_KEY].values():profile.pop('end_time',None)
            users._atomic_write_json(users.USERS_FILE,raw)
        original_bytes=users.USERS_FILE.read_bytes()
        with patch.object(streak,'_now',return_value=now+timedelta(hours=1)):
            again=streak.save_preferences(principal,end_time='24:00',expected_revision=saved['revision'],**values)
        self.assertEqual(saved['revision'],again['revision'])
        self.assertEqual(saved['next_run_at'],again['next_run_at'])
        self.assertEqual('24:00',again['end_time'])
        self.assertEqual(original_bytes,users.USERS_FILE.read_bytes())
