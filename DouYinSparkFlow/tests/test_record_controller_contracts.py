"""Rendered record parameters survive the actual served JavaScript controllers."""
from pathlib import Path
import unittest
from tests import test_picker_experience_browser as picker_fixtures
from tests import test_workspace_lifecycle_browser as login_fixtures
STATIC=Path(__file__).resolve().parents[1]/'webui/static'

class RecordPickerEndpointTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.fixture=picker_fixtures.PickerExperienceBrowserTests();await self.fixture.asyncSetUp();self.addAsyncCleanup(self.fixture.asyncTearDown)
  self.page=self.fixture.page
  await self.page.evaluate("""() => {
   for(const picker of document.querySelectorAll('[data-refresh-url]'))picker.dataset.refreshUrl+='?ref=demo-record&generation=demo-generation';
  }""")
 async def test_served_friend_and_group_controllers_insert_suffix_before_query(self):
  await self.page.add_script_tag(path=str(STATIC/'account-pickers.js'))
  await self.page.wait_for_function('requests.length>=2')
  await self.page.wait_for_function("!document.querySelector('.friend-refresh-button').disabled && !document.querySelector('.group-refresh-button').disabled")
  await self.page.locator('.friend-refresh-button').click();await self.page.locator('.group-refresh-button').click()
  await self.page.wait_for_function('requests.filter(x=>x.post).length===2')
  urls=[r['url'] for r in await self.page.evaluate('requests')]
  for kind in ['friends','groups']:
   self.assertIn('/accounts/1/'+kind+'/refresh/status?ref=demo-record&generation=demo-generation',urls)
   self.assertIn('/accounts/1/'+kind+'/refresh/async?ref=demo-record&generation=demo-generation',urls)

class CookieRecordPayloadTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.fixture=login_fixtures.WorkspaceLifecycleBrowserTests();await self.fixture.asyncSetUp();self.addAsyncCleanup(self.fixture.asyncTearDown)
  self.page=self.fixture.page
  await self.page.evaluate("""() => {
   document.body.insertAdjacentHTML('beforeend',`<section data-cookie-login><textarea data-cookie-input>sessionid=synthetic</textarea><input data-cookie-display-name><input type="checkbox" data-cookie-relogin-mode checked><label data-cookie-relogin-row><select data-cookie-relogin-select><option value="a1" data-unique-id="111" data-login-generation="gen-a1">First demo</option><option value="a3" data-unique-id="111" data-login-generation="gen-a3" selected>Second demo</option></select></label><button data-cookie-login-submit>Save</button><p data-cookie-login-status></p></section>`);
   const prior=window.fetch;window.cookieFields=[];
   window.fetch=(url,options={})=>{if(url==='/accounts/cookies'){cookieFields.push(Object.fromEntries(options.body.entries()));return Promise.resolve(jsonResponse({ok:false,error:'synthetic boundary reached'},{status:400}));}return prior(url,options);};
  }""")
 async def test_served_cookie_controller_submits_platform_id_ref_and_generation(self):
  await self.page.add_script_tag(path=str(STATIC/'login-workspace.js'))
  await self.page.locator('[data-cookie-login-submit]').click();await self.page.wait_for_function('cookieFields.length===1')
  fields=await self.page.evaluate('cookieFields[0]')
  self.assertEqual('111',fields['relogin_unique_id']);self.assertEqual('a3',fields['relogin_account_ref']);self.assertEqual('gen-a3',fields['login_generation'])

if __name__=='__main__':unittest.main()
