"""Synthetic browser tests of the schedule preview response contract."""
import unittest
from pathlib import Path
from playwright.async_api import async_playwright

SCRIPT=Path(__file__).resolve().parents[1]/'webui/static/app-shell.js'

class SchedulePreviewErrors(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p=await async_playwright().start();self.browser=await self.p.chromium.launch();self.page=await self.browser.new_page()
        await self.page.set_content('''<form data-schedule-form data-schedule-preview-url="/preview"><input name="daily_schedule" value="10:00-17:00"><input name="csrf_token" value="synthetic"><button type="button" data-schedule-preview>预览</button><div data-schedule-preview-result hidden></div></form>''')
    async def asyncTearDown(self):
        await self.browser.close();await self.p.stop()
    async def test_session_denial_service_and_validation_are_distinct(self):
        # Load the actual controller on a fresh page per scenario. Fetch is an
        # in-page fake: no real console, account, or messaging endpoint is used.
        cases=[('session', '登录已失效'),('redirect','登录已失效'),('forbidden','页面已过期'),('server','服务器暂时不可用'),('html','预览失败'),('invalid','格式不正确'),('valid','将生效为'),('badstatus','预览失败')]
        for mode,expected in cases:
            with self.subTest(mode=mode):
                page=await self.browser.new_page()
                await page.set_content('''<form data-schedule-form data-schedule-preview-url="/preview"><input name="daily_schedule" value="10:00-17:00"><input name="csrf_token" value="synthetic"><button type="button" data-schedule-preview>预览</button><div data-schedule-preview-result hidden></div></form>''')
                await page.evaluate('''mode => {window.fetch=async()=>{let status=({session:401,forbidden:403,server:500,badstatus:409})[mode]||200;let isJson=['invalid','valid','badstatus'].includes(mode);let body=['valid','badstatus'].includes(mode)?JSON.stringify({ok:true,label:'10:00-17:00'}):mode==='invalid'?JSON.stringify({ok:false,error:'格式不正确'}):'<!doctype html>';let r=new Response(body,{status,headers:{'content-type':isJson?'application/json':'text/html'}});if(mode==='redirect')Object.defineProperty(r,'redirected',{value:true});return r;}}''',mode)
                await page.add_script_tag(path=str(SCRIPT))
                await page.locator('[data-schedule-preview]').click()
                await page.wait_for_timeout(30)
                text=await page.locator('[data-schedule-preview-result]').inner_text()
                self.assertIn(expected,text);self.assertNotIn('Unexpected token',text)
                await page.close()

if __name__=='__main__':unittest.main()
