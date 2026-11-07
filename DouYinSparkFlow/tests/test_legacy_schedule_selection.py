"""Global legacy scheduling must pass due selection without a personal timezone."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import os,unittest
from unittest.mock import patch
from core import personal_streak,refusal_retry,streak_state,tasks
from tests import test_shared_security as fixtures
from utils import config
from webui import users

NOW=datetime(2026,11,4,16,59,tzinfo=ZoneInfo('Asia/Shanghai'))
CONFIG={'messageTemplate':'demo','dailySendWindow':{'enabled':True,'startHour':10,'endHour':17,'scheduleIntervalMinutes':20}}

class LegacyScheduleSelectionTests(unittest.TestCase):
 def setUp(self):
  f=fixtures.SharedConsoleTests();f.setUp();self.addCleanup(f.doCleanups);self.fixture=f
  self.account=dict(f.accounts[0],enabled=True,targets=['demo-friend','群聊：7691234567890123400'])
  config.save_userData([self.account,f.accounts[1]])
  self.owner=users.stable_user_id(users.find_web_user('alice'))
  raw=users._load_raw();raw.pop(personal_streak.STORE_KEY,None);raw['legacy_global_schedule']={self.owner:{'account_refs':['a1']}};users._atomic_write_json(users.USERS_FILE,raw)
  self.env=patch.dict(os.environ,{'SPARKFLOW_MANUAL_RUN':'0','SPARKFLOW_FALLBACK_PHASE':'0','SPARKFLOW_8610_RETRY_ONLY':'0'});self.env.start();self.addCleanup(self.env.stop)

 def grouped(self):
  groups=personal_streak.account_groups(CONFIG,config.get_userData(force_reload=True),manual=False)
  self.assertEqual(1,len(groups));effective,accounts=groups[0];self.assertEqual(1,len(accounts))
  return effective,accounts[0]

 def test_authorized_legacy_group_reaches_due_selection_with_global_policy(self):
  effective,account=self.grouped();self.assertTrue(personal_streak.authorized(account));self.assertIsNone(personal_streak.authorized_schedule_timezone(account))
  with patch.object(refusal_retry,'used_today',wraps=refusal_retry.used_today) as quota:
   due,sent,pending,excluded=tasks._select_due_targets(account,tasks._normalize_send_window(effective),NOW)
  self.assertEqual(account['targets'],due);self.assertEqual(([],[],[]),(sent,pending,excluded))
  self.assertTrue(quota.called);self.assertIsNone(quota.call_args.kwargs['schedule_timezone'])

 def assert_all_scope_gates_denied(self,account):
  self.assertEqual([],tasks._select_due_targets(account,tasks._normalize_send_window(CONFIG),NOW)[0])
  with patch.object(tasks,'datetime',wraps=datetime) as clock:
   clock.now.side_effect=lambda tz:NOW.astimezone(tz)
   self.assertEqual([],tasks._browser_prelaunch_targets(account,account['targets'],False))
   self.assertFalse(tasks._mark_browser_target_in_flight(account,account['targets'][0],run_id='denied-synthetic-run'))

 def test_revoked_legacy_mapping_remains_excluded(self):
  _,account=self.grouped();raw=users._load_raw();raw['legacy_global_schedule']={};users._atomic_write_json(users.USERS_FILE,raw)
  result=tasks._select_due_targets(account,tasks._normalize_send_window(CONFIG),NOW)
  self.assertEqual([],result[0]);self.assertEqual(account['targets'],result[3])
  self.assert_all_scope_gates_denied(account)

 def test_disabled_owner_remains_excluded(self):
  _,account=self.grouped();users.update_web_user('alice',enabled=False)
  self.assert_all_scope_gates_denied(account)

 def test_stale_record_generation_remains_excluded(self):
  _,account=self.grouped();rows=config.get_userData(force_reload=True);rows[0]['login_generation']='changed';config.save_userData(rows)
  self.assert_all_scope_gates_denied(account)

 def test_new_personal_profile_invalidates_captured_legacy_scope(self):
  _,account=self.grouped();personal_streak.save_preferences({'username':'alice','role':'user'},enabled=False,at_time='10:30',timezone_name='Asia/Tokyo',messages=['demo'])
  self.assert_all_scope_gates_denied(account)

 def test_unknown_outcome_and_daily_retry_quota_still_block(self):
  _,account=self.grouped();target=account['targets'][0];streak_state.mark_sent_unverified(account,target,now=NOW)
  self.assertNotIn(target,tasks._select_due_targets(account,tasks._normalize_send_window(CONFIG),NOW)[0])
  other=account['targets'][1];refusal_retry.reserve(account,other,NOW,'demo-reservation')
  self.assert_all_scope_gates_denied(account)

 def test_global_random_due_times_are_preserved(self):
  effective,account=self.grouped();window=tasks._normalize_send_window(effective)
  for target in account['targets']:
   due=tasks._scheduled_send_time(account,target,window,NOW)
   self.assertEqual(tasks._scheduled_send_time(self.account,target,window,NOW),due)
  first=min(tasks._scheduled_send_time(account,t,window,NOW) for t in account['targets'])
  self.assertEqual([],tasks._select_due_targets(account,window,first-timedelta(seconds=1))[0])
  self.assertTrue(tasks._select_due_targets(account,window,first)[0])

 def test_scheduled_entrypoint_reaches_mocked_browser_dispatch(self):
  import asyncio
  from contextlib import nullcontext
  from unittest.mock import AsyncMock
  effective=dict(CONFIG,multiTask=False,taskCount=1,useProtocolSender=False)
  dispatched=[]
  async def boundary(cfg,accounts):
   for account in accounts:
    actual=tasks._browser_prelaunch_targets(account,account['targets'],False)
    self.assertEqual(account['targets'],actual)
    for target in actual:
     self.assertTrue(tasks._mark_browser_target_in_flight(account,target,run_id='synthetic-entry-run'))
     dispatched.append(target)
  sender=AsyncMock(side_effect=boundary)
  with patch.object(tasks,'get_config',return_value=effective),patch.object(tasks,'datetime',wraps=datetime) as clock,patch.object(tasks.send_pause,'should_pause_sending',return_value=False),patch.object(tasks.streak_state,'preflight_account',return_value={'healthy':True}),patch.object(tasks.streak_state,'reconcile_account'),patch.object(tasks,'task_run_lock',return_value=nullcontext()),patch.object(tasks,'run_browser_tasks',sender),patch.object(tasks,'_append_streak_run_report'),patch.object(tasks.notifications,'notify_run_finished'):
   clock.now.side_effect=lambda tz:NOW.astimezone(tz)
   result=asyncio.run(tasks.runTasks())
  self.assertEqual('completed',result);sender.assert_awaited_once();self.assertEqual(self.account['targets'],sender.await_args.args[1][0]['targets']);self.assertEqual(self.account['targets'],dispatched)

 def test_legacy_browser_prelaunch_keeps_authorized_targets(self):
  _,account=self.grouped()
  with patch.object(tasks,'datetime',wraps=datetime) as clock:
   clock.now.side_effect=lambda tz:NOW.astimezone(tz)
   self.assertEqual(account['targets'],tasks._browser_prelaunch_targets(account,account['targets'],False))

 def test_legacy_browser_dispatch_claim_is_persisted_and_read_back(self):
  _,account=self.grouped();target=account['targets'][0]
  with patch.object(tasks,'datetime',wraps=datetime) as clock:
   clock.now.side_effect=lambda tz:NOW.astimezone(tz)
   self.assertTrue(tasks._mark_browser_target_in_flight(account,target,run_id='synthetic-legacy-run'))
  live=config.get_userData(force_reload=True)[0];state=streak_state.target_state(live,target,NOW)
  self.assertEqual('in_flight',state['status']);self.assertEqual('synthetic-legacy-run',state['runId'])

 def test_forged_legacy_marker_cannot_bypass_personal_profile(self):
  personal_streak.save_preferences({'username':'alice','role':'user'},enabled=True,at_time='10:30',timezone_name='Asia/Tokyo',messages=['demo'])
  account=personal_streak.account_groups(CONFIG,config.get_userData(force_reload=True),manual=True)[0][1][0]
  account['_personal_scope']=dict(account['_personal_scope'],legacy_global_schedule=True)
  self.assert_all_scope_gates_denied(account)

 def locked_launch_reached(self,account):
  import asyncio
  from types import SimpleNamespace
  from unittest.mock import AsyncMock
  class LaunchReached(Exception):pass
  browser=SimpleNamespace(new_context=AsyncMock(side_effect=LaunchReached))
  playwright=SimpleNamespace(stop=AsyncMock())
  launcher=AsyncMock(return_value=(playwright,browser))
  with patch.object(tasks,'_sleep_with_log',new_callable=AsyncMock),patch.object(tasks,'_cleanup_browser_session',new_callable=AsyncMock),patch.object(tasks,'get_browser',launcher),patch.object(tasks,'datetime',wraps=datetime) as clock:
   clock.now.side_effect=lambda tz:NOW.astimezone(tz)
   try:asyncio.run(tasks._do_user_task_locked(browser,account,tasks._normalize_send_strategy({}), {'enabled':False},{},'synthetic','direct'))
   except LaunchReached:pass
  if browser.new_context.await_count:
   launcher.assert_awaited_once_with(network_mode='direct',profile_name=tasks._account_profile_name(account))
  else:launcher.assert_not_awaited()
  return browser.new_context.await_count>0

 def test_actual_locked_path_keeps_authorized_legacy_launch(self):
  _,account=self.grouped();self.assertTrue(self.locked_launch_reached(account))

 def test_actual_locked_path_rejects_scope_changes_before_browser_launch(self):
  from copy import deepcopy
  _,account=self.grouped();original_raw=users._load_raw();original_rows=config.get_userData(force_reload=True)
  for case in ['mapping_revoked','owner_disabled','account_disabled','stale_generation','new_profile','forged_owner']:
   raw=deepcopy(original_raw);rows=deepcopy(original_rows);captured=deepcopy(account)
   if case=='mapping_revoked':raw['legacy_global_schedule']={}
   elif case=='owner_disabled':next(u for u in raw['users'] if u['username']=='alice')['enabled']=False
   elif case=='account_disabled':rows[0]['enabled']=False
   elif case=='stale_generation':rows[0]['login_generation']='changed'
   elif case=='new_profile':raw[personal_streak.STORE_KEY]={self.owner:{'enabled':False,'revision':'new'}}
   else:captured['_personal_scope']['owner']='synthetic-wrong-owner'
   users._atomic_write_json(users.USERS_FILE,raw);config.save_userData(rows)
   with self.subTest(case=case):self.assertFalse(self.locked_launch_reached(captured))

if __name__=='__main__':unittest.main()
