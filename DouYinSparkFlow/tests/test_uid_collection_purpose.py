"""No-network regressions for per-operation UID collection."""
import ast
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch, AsyncMock
from core import friend_identity as identity

class PurposePolicyTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.marker=Path(self.temp.name)/'uid-collection-off.flag';self.marker.touch()
  p=patch.object(identity,'uid_collection_marker_path',return_value=self.marker);p.start();self.addCleanup(p.stop)
  p=patch.dict(os.environ,{'SPARKFLOW_UID_COLLECTION':'off'});p.start();self.addCleanup(p.stop)
 def test_patrol_and_refresh_collect_despite_legacy_send_marker(self):
  for purpose in ['patrol','refresh']:
   with self.subTest(purpose=purpose):
    page=Mock();c=identity.UserDetailCollector('demo',purpose=purpose).attach(page)
    self.assertFalse(c.disabled);page.on.assert_called_once();c.detach()
 def test_sending_never_collects_even_if_global_env_is_on(self):
  self.marker.unlink()
  with patch.dict(os.environ,{'SPARKFLOW_UID_COLLECTION':'on'}):
   page=Mock();c=identity.UserDetailCollector('demo',purpose='send').attach(page)
   self.assertTrue(c.disabled);page.on.assert_not_called();self.assertEqual((),c.pending_tasks())
   unknown_page=Mock();unknown=identity.UserDetailCollector('demo',purpose='typo').attach(unknown_page)
   self.assertTrue(unknown.disabled);unknown_page.on.assert_not_called()
 def test_new_emergency_marker_stops_patrol_and_refresh(self):
  (self.marker.parent/'uid-collection-all-off.flag').touch()
  for purpose in ['patrol','refresh','send']:
   page=Mock();c=identity.UserDetailCollector('demo',purpose=purpose).attach(page)
   self.assertTrue(c.disabled);page.on.assert_not_called()

 def test_default_legacy_collector_keeps_previous_marker_behavior(self):
  page=Mock();c=identity.UserDetailCollector('demo').attach(page)
  self.assertTrue(c.disabled);page.on.assert_not_called()
 def test_operation_policy_is_per_instance_not_shared_state(self):
  read_page,send_page=Mock(),Mock()
  read=identity.UserDetailCollector('demo',purpose='patrol').attach(read_page)
  send=identity.UserDetailCollector('demo',purpose='send').attach(send_page)
  self.assertFalse(read.disabled);self.assertTrue(send.disabled)
  read_page.on.assert_called_once();send_page.on.assert_not_called();read.detach()
 def test_legacy_marker_read_error_preserves_env_fallback(self):
  with patch.object(identity,'uid_collection_marker_path',side_effect=OSError('synthetic-read-error')),patch.dict(os.environ,{'SPARKFLOW_UID_COLLECTION':'on'}):
   self.assertTrue(identity.uid_collection_enabled())
   self.assertFalse(identity.uid_collection_enabled('patrol'))

 def test_each_production_caller_declares_its_operation(self):
  root=Path(identity.__file__).parent
  expected={'tasks.py':'send','friends.py':'refresh','patrol.py':'patrol'}
  for name,purpose in expected.items():
   tree=ast.parse((root/name).read_text());calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and ((isinstance(n.func,ast.Attribute) and n.func.attr=='UserDetailCollector') or (isinstance(n.func,ast.Name) and n.func.id=='UserDetailCollector'))]
   self.assertEqual(1,len(calls),name)
   self.assertTrue(any(k.arg=='purpose' and isinstance(k.value,ast.Constant) and k.value.value==purpose for k in calls[0].keywords),name)

class PassiveReadTests(unittest.IsolatedAsyncioTestCase):
 async def test_patrol_listener_parses_received_response_without_new_requests(self):
  with tempfile.TemporaryDirectory() as temp:
   marker=Path(temp)/'uid-collection-off.flag';marker.touch();page=Mock()
   with patch.object(identity,'uid_collection_marker_path',return_value=marker),patch.dict(os.environ,{'SPARKFLOW_UID_COLLECTION':'off'}):
    c=identity.UserDetailCollector('demo',purpose='patrol').attach(page)
   response=Mock(url='https://example.invalid'+identity.USER_DETAIL_PATH)
   response.json=AsyncMock(return_value={'status_code':0,'user_list':[{'user_id':'1001001','user':{'nickname':'Synthetic friend','SecretUseId':'synthetic-sec'}}]})
   listener=page.on.call_args.args[1];listener(response)
   await c.drain()
   self.assertEqual(1,response.json.await_count)
   self.assertTrue(c.entries());page.request.assert_not_called();self.assertTrue(await c.aclose())

if __name__=='__main__':unittest.main()
