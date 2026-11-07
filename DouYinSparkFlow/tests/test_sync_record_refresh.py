"""Internal saved snapshots count as a supplied generation, including empty."""
import unittest
from unittest.mock import AsyncMock,patch
from tests import test_account_record_ref_routing as fixtures
from core import friends
from webui import app as app_module
from utils import config

class SyncRecordRefreshTests(unittest.TestCase):
 def setUp(self):
  self.fixture=fixtures.AccountRecordRefRoutingTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
 def test_current_second_record_sync_refresh_can_persist_its_cache(self):
  f=self.fixture
  with patch.object(app_module,'fetch_account_friends',new=AsyncMock(return_value=friends.FriendScanResult(['Fresh demo friend'],complete=True))),patch.object(app_module,'record_friend_scan',return_value=False):
   response=f.alice.post('/accounts/111/friends/refresh?ref=a3&generation=gen-a3',data={'csrf_token':f.token})
  self.assertEqual(200,response.status_code,response.text)
  rows=f.stored();self.assertEqual(['Fresh demo friend'],rows['a3']['friends_cache']);self.assertNotIn('Fresh demo friend',rows['a1'].get('friends_cache',[]))

if __name__=='__main__':unittest.main()
