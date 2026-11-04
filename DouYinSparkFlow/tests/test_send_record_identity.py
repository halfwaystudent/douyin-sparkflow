"""Record identity must survive send-console dispatch and durable barriers."""
from copy import deepcopy
from datetime import datetime,timezone
from html.parser import HTMLParser
import unittest
from unittest.mock import AsyncMock,patch
from tests import test_account_record_ref_routing as fixtures
from core import tasks,streak_state
from webui import app as app_module
from utils import config

class RecordSenderIdentityTests(unittest.TestCase):
 def setUp(self):
  self.rows=[{'account_ref':'a1','unique_id':'111','username':'First','login_generation':'gen-a1','targets':['Demo']},{'account_ref':'a3','unique_id':'111','username':'Second','login_generation':'gen-a3','targets':['Demo']}]
 def test_sender_helpers_choose_exact_second_ref(self):
  for fn in [tasks._find_matching_account,streak_state.find_matching_account]:
   self.assertIs(self.rows[1],fn(self.rows,dict(self.rows[1])))
 def test_sender_helpers_never_fallback_after_ref_or_generation_changes(self):
  for fn in [tasks._find_matching_account,streak_state.find_matching_account]:
   for changed in [dict(self.rows[1],account_ref='removed'),dict(self.rows[1],login_generation='old'),dict(self.rows[1],unique_id='222')]:
    with self.subTest(fn=fn.__name__,changed=changed):self.assertIsNone(fn(self.rows,changed))
 def test_ambiguous_uid_without_ref_is_not_a_sender_identity(self):
  for fn in [tasks._find_matching_account,streak_state.find_matching_account]:self.assertIsNone(fn(self.rows,{'unique_id':'111'}))
 def test_pre_enter_barrier_and_readback_belong_to_second_record(self):
  disk=deepcopy(self.rows);user=dict(self.rows[1]);now=datetime.now(timezone.utc).isoformat(timespec='seconds')
  def update(mutator,**kwargs):result,changed=mutator(disk);return deepcopy(result)
  with patch.object(tasks,'update_user_data',side_effect=update),patch.object(tasks,'get_userData',side_effect=lambda **kw:deepcopy(disk)):
   self.assertTrue(tasks._persist_browser_pre_enter_unknown(user,'Demo','demo',now))
  self.assertFalse(disk[0].get('target_states'))
  self.assertEqual('pre_enter_outcome_unknown_barrier',streak_state.target_state(disk[1],'Demo')['lastEvidenceDetail'])

class SendConsoleRecordTests(unittest.TestCase):
 def test_every_target_action_renders_record_and_generation_fields(self):
  from pathlib import Path
  import re
  template=(Path(__file__).resolve().parents[1]/'webui/templates/send_console.html').read_text()
  forms=[body for body in re.findall(r'<form\b(.*?)</form>',template,re.S) if '/retry-target' in body or '/mark-target-unconfirmed' in body]
  self.assertEqual(6,len(forms))
  for body in forms:
   self.assertIn('name="account_ref"',body);self.assertIn('name="login_generation"',body)
   self.assertIn('?ref=',body);self.assertIn('generation=',body)
 def setUp(self):
  self.fixture=fixtures.AccountRecordRefRoutingTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
 def test_stale_retry_refuses_before_sender_starts(self):
  f=self.fixture;rows=f.stored();rows['a3']['login_generation']='replaced';config.save_userData(list(rows.values()))
  with patch.object(app_module,'run_browser_tasks',new=AsyncMock()) as run:
   response=f.alice.post('/accounts/111/retry-target',data=f.form(target='second target'),follow_redirects=False)
  self.assertEqual(409,response.status_code,response.text);run.assert_not_awaited()
 def test_stale_mark_cannot_rewrite_a_replaced_record(self):
  f=self.fixture;rows=f.stored();rows['a3']['login_generation']='replaced';config.save_userData(list(rows.values()));before=deepcopy(f.stored())
  response=f.alice.post('/accounts/111/mark-target-unconfirmed',data=f.form(target='second target'),follow_redirects=False)
  self.assertEqual(409,response.status_code,response.text);self.assertEqual(before,f.stored())
 def test_current_second_record_retry_reaches_only_its_mock_sender(self):
  f=self.fixture;admin=f.fixture.login('owner','Owner-secret-2026!');csrf=f.fixture.token(admin)
  with patch.object(app_module,'run_browser_tasks',new=AsyncMock()) as run,patch.object(app_module,'_append_streak_run_report'):
   response=admin.post('/accounts/111/retry-target',data=dict(f.form(target='second target'),csrf_token=csrf),follow_redirects=False)
  self.assertEqual(303,response.status_code,response.text);run.assert_awaited_once();self.assertEqual('a3',run.await_args.args[1][0]['account_ref'])

if __name__=='__main__':unittest.main()
