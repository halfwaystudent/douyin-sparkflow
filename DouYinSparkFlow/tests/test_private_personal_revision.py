"""A private deployment must reject missing optimistic-concurrency tokens too."""
import os
import unittest
from unittest.mock import patch
from core import personal_streak
from tests import test_shared_security as fixtures

class PersonalRevisionBoundaryTests(unittest.TestCase):
 def setUp(self):
  self.fixture=fixtures.SharedConsoleTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
  self.client=self.fixture.login();self.csrf=self.fixture.token(self.client)
  self.principal={'username':'alice','role':'user'}
  self.form={'csrf_token':self.csrf,'edit_revision':'','time':'10:00','end_time':'17:00','timezone':'Asia/Shanghai','messages':'Saved message','enabled':'on'}
  self.assertEqual(303,self.client.post('/preferences/streak',data=self.form,follow_redirects=False).status_code)
 def test_private_old_form_without_revision_cannot_overwrite_saved_plan(self):
  before=personal_streak.preferences(self.principal)
  form={k:v for k,v in self.form.items() if k!='edit_revision'};form.update(time='11:00',messages='Unsaved draft')
  with patch.dict(os.environ,{'SPARKFLOW_SHARED_MODE':'0'}):
   response=self.client.post('/preferences/streak',data=form,follow_redirects=False)
  self.assertEqual(409,response.status_code)
  self.assertIn('Unsaved draft',response.text)
  self.assertEqual(before,personal_streak.preferences(self.principal))
 def test_private_ajax_without_revision_preserves_plan_and_reports_conflict(self):
  before=personal_streak.preferences(self.principal)
  form={k:v for k,v in self.form.items() if k!='edit_revision'};form.update(messages='Unsaved AJAX draft')
  with patch.dict(os.environ,{'SPARKFLOW_SHARED_MODE':'0'}):
   response=self.client.post('/preferences/streak',data=form,headers={'X-Requested-With':'XMLHttpRequest'})
  self.assertEqual(409,response.status_code);self.assertEqual('edit_conflict',response.json()['category'])
  self.assertEqual(before,personal_streak.preferences(self.principal))
 def test_private_current_revision_can_save_inherited_messages(self):
  before=personal_streak.preferences(self.principal)
  form=dict(self.form,edit_revision=before['revision'],messages='',time='11:00')
  with patch.dict(os.environ,{'SPARKFLOW_SHARED_MODE':'0'}):
   response=self.client.post('/preferences/streak',data=form,follow_redirects=False)
  self.assertEqual(303,response.status_code)
  after=personal_streak.preferences(self.principal);self.assertEqual([],after['messages']);self.assertEqual('11:00',after['time'])
  self.assertNotEqual(before['revision'],after['revision'])

if __name__=='__main__':unittest.main()
