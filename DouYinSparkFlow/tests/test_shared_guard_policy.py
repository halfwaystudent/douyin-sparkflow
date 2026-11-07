"""One shared guard policy for cron, personal and Web sender entrypoints."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from core import friend_chat_guard, tasks

class SharedGuardMarkerTests(unittest.TestCase):
 def setUp(self):
  self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
  self.marker=Path(self.directory.name)/'friend-guard-on.flag'
  p=patch.object(friend_chat_guard,'_GUARD_MARKER',self.marker,create=True);p.start();self.addCleanup(p.stop)
  p=patch.dict(os.environ,{},clear=False);p.start();self.addCleanup(p.stop)
  os.environ.pop('SPARKFLOW_FRIEND_GUARD',None)
 def test_marker_enables_guard_without_runner_environment(self):
  self.marker.touch()
  self.assertTrue(friend_chat_guard.guard_enabled())
 def test_absent_marker_retains_default_off(self):
  self.assertFalse(friend_chat_guard.guard_enabled())
 def test_explicit_off_still_overrides_marker(self):
  self.marker.touch()
  for raw in ['off','0','false','anything-else']:
   with self.subTest(raw=raw),patch.dict(os.environ,{'SPARKFLOW_FRIEND_GUARD':raw}):
    self.assertFalse(friend_chat_guard.guard_enabled())
 def test_environment_on_does_not_require_marker(self):
  with patch.dict(os.environ,{'SPARKFLOW_FRIEND_GUARD':'on'}):
   self.assertTrue(friend_chat_guard.guard_enabled())
 def test_unreadable_marker_never_disables_identity_check(self):
  with patch.object(Path,'is_file',side_effect=PermissionError('synthetic unreadable marker')):
   self.assertTrue(friend_chat_guard.guard_enabled())
 def test_marker_changes_take_effect_without_mutating_process_environment(self):
  before=dict(os.environ);self.assertFalse(friend_chat_guard.guard_enabled())
  self.marker.touch();self.assertTrue(friend_chat_guard.guard_enabled())
  self.marker.unlink();self.assertFalse(friend_chat_guard.guard_enabled())
  self.assertEqual(before,dict(os.environ))
 def test_sender_identity_check_uses_marker_without_launcher(self):
  self.marker.touch();verified=AsyncMock(return_value=(True,''))
  with patch.object(tasks,'_find_matching_account',return_value={'targets':['Demo friend']}),patch.object(friend_chat_guard,'verify_friend',verified):
   asyncio.run(tasks._require_friend_chat(Mock(),{'targets':['Demo friend']},'Demo friend',arm=True,wait=True))
  self.assertEqual(1,verified.await_count)
 def test_web_retry_real_route_sees_shared_policy(self):
  from contextlib import nullcontext
  from fastapi.testclient import TestClient
  from webui import app as web_app
  self.marker.touch();account={'account_ref':'audit-record','unique_id':'audit-id','username':'Demo','targets':['Demo friend'],'cookies':[]};observed=[]
  async def fake_send(*args,**kwargs):observed.append(friend_chat_guard.guard_enabled())
  with patch.object(web_app,'current_principal',return_value={'role':'admin','username':'admin'}),patch.object(web_app,'validate_csrf',return_value=True),patch.object(web_app,'ensure_account_refs',side_effect=lambda rows:(rows,False)),patch.object(web_app,'account_by_unique_id',return_value=account),patch.object(web_app,'can_access_account',return_value=True),patch.object(web_app,'task_run_lock_status',return_value={'running':False}),patch.object(web_app,'task_run_lock',return_value=nullcontext()),patch.object(web_app,'get_config',return_value={'useProtocolSender':False}),patch.object(web_app,'run_browser_tasks',new=AsyncMock(side_effect=fake_send)),patch.object(web_app,'get_userData',return_value=[account]),patch.object(web_app,'find_account',return_value=account),patch.object(web_app,'_append_streak_run_report'),patch.object(web_app,'_target_sent_today',return_value=False),patch.object(web_app,'_target_unconfirmed_today',return_value=False):
   response=TestClient(web_app.app).post('/accounts/audit-id/retry-target',data={'target':'Demo friend','csrf_token':'demo'},follow_redirects=False)
  self.assertEqual(303,response.status_code);self.assertEqual([True],observed)

if __name__=='__main__':unittest.main()
