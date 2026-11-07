"""A bound target's receipt must survive canonical rekey during recovery."""
import json,tempfile,unittest,os
from pathlib import Path
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from core import tasks as T,streak_state as S,recovery_queue as Q
from utils.config import update_user_data

class BoundRefusalPersistenceTests(unittest.TestCase):
 def test_bound_refusal_commits_state_receipt_and_waiting_queue_together(self):
  now=datetime(2026,11,6,12,22,43,tzinfo=timezone.utc)
  for code in [8101,8610]:
   with self.subTest(code=code),tempfile.TemporaryDirectory() as d:
    account={'account_ref':'synthetic-account','unique_id':'123456','username':'synthetic','login_generation':'generation-one','enabled':True,'targets':['friend'],'target_uid_bindings':{'friend':{'uid':'654321','secUid':'synthetic-sec-uid','status':'bound'}}}
    S.mark_sent_unverified(account,'friend',run_id='synthetic-attempt',now=now,detail='pre_enter_outcome_unknown_barrier')
    receipt={'call':'message_send','ok':False,'httpStatus':200,'protoParsed':True,'protoOk':False,'jsonOk':False,'protoStatusCode':0,'protoSendStatus':0 if code==8101 else 3,'protoCheckCode':0 if code==8101 else 2,'protoCheckCodes':{'status_code':code,'raw_check_code':1 if code==8101 else 2},'protoRefusalUnderstood':code==8101,'rejection':{'key':'status_code','value':code,'source':'raw'}}
    path=Path(d)/'accounts.json';path.write_text(json.dumps([account]))
    with patch.dict(os.environ,{'TZ':'Asia/Shanghai','SPARKFLOW_TIMEZONE':'Asia/Shanghai'}),patch.object(T,'update_user_data',side_effect=lambda fn,**kw:update_user_data(fn,path=path,force_reload=True)),patch.object(Q,'get_config',return_value={'automaticRecovery':{'enabled':True}}),patch.object(Q.random,'randint',return_value=45):
     T._persist_browser_send_failure(account,'friend','synthetic message','send_rejected','synthetic refusal',now.isoformat(timespec='seconds'),server_receipt=receipt,override_sent_unverified=True)
     saved=json.loads(path.read_text())[0];state=S.target_state(saved,'friend',now);failure=S.failure_entry(saved,'friend',now.tzinfo);queue=Q.target_entry(saved,'friend',now)
     self.assertEqual('failed_terminal',state['status']);self.assertEqual('send_rejected',state['lastErrorCategory']);self.assertEqual(receipt,failure['serverReceipt'])
     self.assertEqual('waiting',queue['state']);self.assertEqual('refusal',queue['kind']);self.assertEqual(now+timedelta(minutes=45),datetime.fromisoformat(queue['nextAttemptAt']))
     self.assertFalse(Q.due(saved,'friend',now));self.assertTrue(Q.due(saved,'friend',now+timedelta(minutes=45)))
     self.assertEqual(1,len(saved['failure_queue']))
 def test_unknown_is_not_reopened_without_explicit_refusal_override(self):
  now=datetime(2026,11,6,12,22,43,tzinfo=timezone.utc)
  account={'unique_id':'demo','username':'demo','targets':['friend']};S.mark_sent_unverified(account,'friend',now=now)
  path=None
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'accounts.json';path.write_text(json.dumps([account]));before=path.read_bytes()
   with patch.object(T,'update_user_data',side_effect=lambda fn,**kw:update_user_data(fn,path=path,force_reload=True)),patch.object(Q,'get_config',return_value={'automaticRecovery':{'enabled':True}}):
    T._persist_browser_send_failure(account,'friend','synthetic','unknown','missing receipt',now.isoformat(timespec='seconds'))
   self.assertEqual(before,path.read_bytes())

if __name__=='__main__':unittest.main()
