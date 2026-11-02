"""An interrupted save is observed by receipt, never replayed by the browser."""

import unittest

from tests import test_workspace_lifecycle_browser as fixtures


ASYNC_SAVE = r'''() => {
  window.saveState = 'running'; window.saveError = '平台身份校验未完成'; window.statusHttp = 200;
  window.operationId = ''; window.receiptTicket = activeWorkspace.ticket;
  window.savedResult = {ok:true,account:{account_ref:'a1',unique_id:'111',username:'Alice',login_generation:'one'},workspace:{state:'closed',active:false,ticket:''},verification:'verified',friend_refresh:'started'};
  const original = reply;
  window.reply = url => {
    if (url === '/login-desktop/save') {
      operationId = events[events.length-1].requestId;
      return jsonResponse({ok:true,state:'running',operation_id:operationId,ticket:receiptTicket,status_url:'/login-desktop/save/status?operation_id='+operationId},{status:202});
    }
    if (url.startsWith('/login-desktop/save/status?')) {
      if (statusHttp !== 200) return jsonResponse({ok:false,error:saveError,binding_pending:true},{status:statusHttp});
      if (saveState === 'done') serverWorkspace = {...savedResult.workspace};
      return jsonResponse({ok:true,state:saveState,operation_id:operationId,ticket:receiptTicket,
        result:saveState==='done'?savedResult:saveState==='failed'?{ok:false,error:saveError,category:'identity_unavailable',binding_pending:false}:saveState==='uncertain'?{ok:false,error:saveError}:null,
        http_status:saveState==='failed'?400:200});
    }
    return original(url);
  };
}'''


class AsyncSaveBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.WorkspaceLifecycleBrowserTests()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.page = self.fixture.page
        await self.page.evaluate(ASYNC_SAVE)
        await self.fixture.app()
        await self.fixture.open()

    async def save(self):
        await self.page.locator('.login-desktop-save').click()
        await self.page.wait_for_function("events.some(event=>event.url.startsWith('/login-desktop/save/status?'))")

    async def posts(self):
        return [event for event in await self.fixture.calls('/login-desktop/save') if event['method'] == 'POST']

    async def done(self):
        await self.page.evaluate("saveState='done'")
        await self.page.clock.run_for(2100)
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent === '账号已保存'")

    async def test_running_receipt_is_polled_and_success_keeps_original_next_step(self):
        await self.save()
        self.assertTrue(await self.page.locator('.login-desktop-save').is_disabled())
        await self.page.clock.run_for(6000)
        self.assertEqual(1, len(await self.posts()))
        await self.done()
        posts = await self.posts()
        self.assertEqual('1', posts[0]['asyncSave'])
        self.assertRegex(posts[0]['requestId'], r'^[a-f0-9]{32}$')
        self.assertEqual('/accounts?_anchor=account-111', await self.page.locator('[data-binding-next]').get_attribute('href'))
        self.assertTrue(await self.page.locator('[data-recheck-login-save]').is_hidden())
        checks = await self.fixture.calls('/login-desktop/save/status?')
        self.assertTrue(all('operation_id='+posts[0]['requestId'] in event['url'] for event in checks))

    async def test_lost_202_is_checked_using_original_request_id_without_reposting(self):
        await self.page.evaluate("pending['/login-desktop/save']=[]")
        await self.page.locator('.login-desktop-save').click()
        await self.page.wait_for_function("pending['/login-desktop/save'].length===1")
        await self.page.evaluate("operationId=pending['/login-desktop/save'][0].record.requestId;pending['/login-desktop/save'].shift().reject(new TypeError('response lost'));delete pending['/login-desktop/save']")
        await self.page.wait_for_function("events.some(event=>event.url.startsWith('/login-desktop/save/status?'))")
        await self.done()
        self.assertEqual(1, len(await self.posts()))

    async def test_uncertain_result_has_single_flight_get_only_recheck(self):
        await self.page.evaluate("saveState='uncertain'")
        await self.save()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='保存结果待核对'")
        self.assertIn('暂时没有记录，也不代表保存失败', await self.page.locator('[data-binding-result-message]').inner_text())
        self.assertIn('平台身份校验未完成', await self.page.locator('[data-binding-result-message]').inner_text())
        await self.page.evaluate("pending['/login-desktop/save/status?']=[]")
        count = len(await self.fixture.calls('/login-desktop/save/status?'))
        await self.page.locator('[data-recheck-login-save]').click()
        await self.page.locator('[data-recheck-login-save]').dispatch_event('click')
        self.assertEqual(count+1, len(await self.fixture.calls('/login-desktop/save/status?')))
        await self.page.evaluate("pending['/login-desktop/save/status?'].shift().resolve(jsonResponse({ok:true,state:'done',operation_id:operationId,ticket:receiptTicket,result:savedResult,http_status:200}));delete pending['/login-desktop/save/status?']")
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='账号已保存'")
        self.assertEqual(1, len(await self.posts()))

    async def test_known_failure_keeps_specific_error_and_restores_current_step(self):
        await self.page.evaluate("saveState='failed'")
        await self.save()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='保存未完成'")
        self.assertIn('平台身份校验未完成', await self.page.locator('[data-binding-result-message]').inner_text())
        self.assertFalse(await self.page.locator('.login-desktop-save').is_disabled())
        self.assertTrue(await self.page.locator('[data-recheck-login-save]').is_hidden())

    async def test_prewrite_identity_503_keeps_qr_and_ticket_then_retry_succeeds(self):
        await self.page.clock.run_for(101)
        await self.page.wait_for_function("!document.querySelector('[data-login-qr]').hidden")
        image_before = await self.page.locator('[data-login-qr]').get_attribute('src')
        await self.page.evaluate(r'''() => {
          const original=reply;
          window.reply=url=>url==='/login-desktop/save' && events.filter(event=>event.url==='/login-desktop/save').length===1
            ?jsonResponse({ok:false,identityState:'unverified',category:'identity_unverified',retryable:true,binding_pending:false,error:'身份接口暂不可用'},{status:503})
            :original(url);
        }''')
        await self.page.locator('.login-desktop-save').click()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='保存未完成'")
        self.assertIn('身份接口暂不可用', await self.page.locator('[data-binding-result-message]').inner_text())
        self.assertEqual([], await self.fixture.calls('/login-desktop/save/status?'))
        self.assertEqual(fixtures.TICKET, await self.page.locator('#login-desktop-controls').get_attribute('data-workspace-ticket'))
        self.assertEqual(image_before, await self.page.locator('[data-login-qr]').get_attribute('src'))
        self.assertFalse(await self.page.locator('.login-desktop-save').is_disabled())
        self.assertFalse(await self.page.locator('[data-login-qr]').is_hidden())
        await self.page.evaluate("saveState='done'")
        await self.page.locator('.login-desktop-save').click()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='账号已保存'")
        posts = await self.posts()
        self.assertEqual(2, len(posts))
        self.assertEqual(posts[0]['ticket'], posts[1]['ticket'])
        self.assertNotEqual(posts[0]['requestId'], posts[1]['requestId'])

    async def test_async_failed_identity_503_restores_step_without_rescanning(self):
        await self.page.evaluate(r'''() => {
          const original=reply;
          window.reply=url=>url.startsWith('/login-desktop/save/status?') && events.filter(event=>event.url==='/login-desktop/save').length===1
            ?jsonResponse({ok:true,state:'failed',operation_id:operationId,ticket:receiptTicket,http_status:503,result:{ok:false,identityState:'unverified',category:'identity_unverified',retryable:true,binding_pending:false,error:'身份接口暂不可用'}})
            :original(url);
        }''')
        await self.save()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='保存未完成'")
        self.assertIn('身份接口暂不可用', await self.page.locator('[data-binding-result-message]').inner_text())
        self.assertFalse(await self.page.locator('.login-desktop-save').is_disabled())
        self.assertEqual(fixtures.TICKET, await self.page.locator('#login-desktop-controls').get_attribute('data-workspace-ticket'))
        await self.page.evaluate("saveState='done'")
        await self.page.locator('.login-desktop-save').click()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='账号已保存'")
        self.assertEqual(1, len(await self.fixture.calls('/login-desktop/open')))
        self.assertEqual(2, len(await self.posts()))

    async def test_identity_confirmation_uses_a_fresh_request_id(self):
        await self.page.evaluate(r'''() => {
          window.confirm=()=>true;
          const original=reply;
          window.reply=url=>{
            if(url.startsWith('/login-desktop/save/status?') && events.filter(event=>event.url==='/login-desktop/save').length===1)
              return jsonResponse({ok:true,state:'failed',operation_id:operationId,ticket:receiptTicket,http_status:409,result:{ok:false,error:'需要确认原账号',identity_confirmation:{token:'single-use-confirmation',username:'Alice',platform_uid:'111'}}});
            if(url.startsWith('/login-desktop/save/status?')) saveState='done';
            return original(url);
          };
        }''')
        await self.save()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='账号已保存'")
        posts = await self.posts()
        self.assertEqual(2, len(posts))
        self.assertNotEqual(posts[0]['requestId'], posts[1]['requestId'])
        self.assertEqual('single-use-confirmation', posts[1]['confirmation'])

    async def test_same_name_confirmation_uses_fresh_request_id_and_keeps_target(self):
        await self.page.evaluate(r'''() => {
          window.confirm=()=>true;
          const original=reply;
          window.reply=url=>{
            if(url.startsWith('/login-desktop/save/status?') && events.filter(event=>event.url==='/login-desktop/save').length===1)
              return jsonResponse({ok:true,state:'failed',operation_id:operationId,ticket:receiptTicket,http_status:409,result:{ok:false,error:'已有同名账号',duplicate_candidate:{account_ref:'a1',username:'Alice'}}});
            if(url.startsWith('/login-desktop/save/status?')) saveState='done';
            return original(url);
          };
        }''')
        await self.save()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='账号已保存'")
        posts = await self.posts()
        self.assertEqual(2, len(posts))
        self.assertNotEqual(posts[0]['requestId'], posts[1]['requestId'])
        self.assertEqual('a1', posts[1]['mergeWith'])
        self.assertEqual(posts[0]['ticket'], posts[1]['ticket'])

    async def test_cancelled_same_name_confirmation_does_not_submit_again(self):
        await self.page.evaluate(r'''() => {
          window.confirm=()=>false;
          const original=reply;
          window.reply=url=>url.startsWith('/login-desktop/save/status?')
            ?jsonResponse({ok:true,state:'failed',operation_id:operationId,ticket:receiptTicket,http_status:409,result:{ok:false,error:'已有同名账号',duplicate_candidate:{account_ref:'a1',username:'Alice'}}})
            :original(url);
        }''')
        await self.save()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='已取消保存'")
        self.assertEqual(1, len(await self.posts()))
        self.assertFalse(await self.page.locator('.login-desktop-save').is_disabled())

    async def test_revoked_session_never_renders_old_account_result(self):
        await self.page.evaluate("statusHttp=401")
        await self.save()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='保存结果待核对'")
        self.assertIn('会话或权限已变化', await self.page.locator('[data-binding-result-message]').inner_text())
        self.assertNotIn('已保存账号 Alice', await self.page.locator('[data-binding-result-message]').inner_text())
        self.assertEqual(1, len(await self.posts()))

    async def test_status_timeout_is_bounded_and_manual_recheck_can_finish(self):
        await self.page.evaluate("pending['/login-desktop/save/status?']=[]")
        await self.save()
        await self.page.clock.run_for(11999)
        self.assertEqual(1, len(await self.fixture.calls('/login-desktop/save/status?')))
        await self.page.clock.run_for(2)
        self.assertEqual(1, len([event for event in await self.page.evaluate('aborts') if '/save/status?' in event['url']]))
        await self.page.clock.run_for(168100)
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='保存结果待核对'")
        self.assertIn('3 分钟', await self.page.locator('[data-binding-result-message]').inner_text())
        self.assertEqual(1, len(await self.posts()))
        await self.page.evaluate("delete pending['/login-desktop/save/status?'];saveState='done'")
        await self.page.locator('[data-recheck-login-save]').click()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='账号已保存'")
        self.assertEqual(1, len(await self.posts()))

    async def test_late_result_for_another_ticket_never_overwrites_current_workspace(self):
        await self.page.evaluate("pending['/login-desktop/save/status?']=[];ignoreAbort=true")
        await self.save()
        await self.page.evaluate("serverWorkspace={...activeWorkspace,ticket:'ticket-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'}")
        await self.page.clock.run_for(30050)
        await self.page.evaluate("pending['/login-desktop/save/status?'].shift().resolve(jsonResponse({ok:true,state:'done',operation_id:operationId,ticket:receiptTicket,result:savedResult,http_status:200}))")
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='保存结果待核对'")
        self.assertIn('工作区已切换', await self.page.locator('[data-binding-result-message]').inner_text())
        self.assertEqual(fixtures.OTHER_TICKET, await self.page.locator('#login-desktop-controls').get_attribute('data-workspace-ticket'))
        self.assertNotIn('已保存账号 Alice', await self.page.locator('[data-binding-result-message]').inner_text())

    async def test_back_forward_restore_exposes_check_button_without_resubmitting(self):
        await self.page.evaluate("pending['/login-desktop/save/status?']=[]")
        await self.save()
        await self.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))")
        self.assertEqual([], await self.fixture.calls('/release'))
        await self.page.evaluate("delete pending['/login-desktop/save/status?'];saveState='done';window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='保存结果待核对'")
        await self.page.locator('[data-recheck-login-save]').click()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='账号已保存'")
        self.assertEqual(1, len(await self.posts()))

    async def test_missing_receipt_is_uncertain_and_can_be_checked_again(self):
        await self.page.evaluate("statusHttp=404;saveError='任务记录已过期'")
        await self.save()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='保存结果待核对'")
        self.assertIn('暂未查到', await self.page.locator('[data-binding-result-message]').inner_text())
        self.assertTrue(await self.page.locator('.login-desktop-save').is_disabled())
        await self.page.evaluate("statusHttp=200;saveState='done'")
        await self.page.locator('[data-recheck-login-save]').click()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='账号已保存'")
        self.assertEqual(1, len(await self.posts()))

    async def test_replaced_login_or_revoked_session_keeps_specific_review_feedback(self):
        await self.page.evaluate("statusHttp=409;saveError='这个账号的登录已更新，请查看最新结果'")
        await self.save()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent==='保存结果待核对'")
        self.assertIn('登录已更新', await self.page.locator('[data-binding-result-message]').inner_text())
        self.assertEqual(1, len(await self.posts()))

    async def test_existing_running_receipt_is_observed_without_new_save_execution(self):
        await self.page.evaluate(r'''() => {
          const original=reply;
          window.reply=url=>{
            if(url==='/login-desktop/save') { operationId='previous-save-id-123456';return jsonResponse({ok:false,error:'上一笔保存仍在处理',operation_id:operationId,binding_pending:true},{status:409}); }
            return original(url);
          };
        }''')
        await self.save()
        await self.done()
        self.assertEqual(1, len(await self.posts()))
        self.assertTrue(all('previous-save-id-123456' in event['url'] for event in await self.fixture.calls('/login-desktop/save/status?')))

    async def test_qr_requests_carry_ticket_header_without_logging_it_in_url(self):
        await self.page.clock.run_for(101)
        codes = await self.fixture.calls('/login-desktop/qr?')
        self.assertEqual(1, len(codes))
        self.assertEqual(fixtures.TICKET, codes[0]['headerTicket'])
        self.assertNotIn(fixtures.TICKET, codes[0]['url'])
