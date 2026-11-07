"""Only login_check gets a minute; both nested guards must use it."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch
from core import tasks


class LoginCheckMinuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_nested_login_check_has_two_sixty_second_guards(self):
        real_wait=asyncio.wait
        budgets=[]
        async def wait(operations, *, timeout=None, **kwargs):
            budgets.append(timeout)
            return await real_wait(operations,timeout=timeout,**kwargs)
        page=Mock()
        page.locator.return_value.first.count=AsyncMock(return_value=0)
        page.locator.return_value.count=AsyncMock(return_value=0)
        with patch.object(tasks.asyncio,'wait',side_effect=wait), \
                patch.object(tasks,'_body_text_sample',new=AsyncMock(return_value='')):
            await tasks._run_browser_stage('login_check',
                tasks.ensure_not_login_required(page,'synthetic','open_chat_page'))
        self.assertEqual(budgets,[60,60])

    async def test_other_stages_and_explicit_budgets_do_not_change(self):
        real_wait=asyncio.wait
        budgets=[]
        async def wait(operations, *, timeout=None, **kwargs):
            budgets.append(timeout)
            return await real_wait(operations,timeout=timeout,**kwargs)
        with patch.object(tasks.asyncio,'wait',side_effect=wait):
            await tasks._run_browser_stage('input_before_type',asyncio.sleep(0))
            await tasks._run_browser_stage('close_context',asyncio.sleep(0),timeout_seconds=5)
            await tasks._run_browser_stage('login_check',asyncio.sleep(0),timeout_seconds=7)
        self.assertEqual(budgets,[30,5,7])

    async def test_scaled_thirty_to_sixty_interval_can_finish_but_still_times_out(self):
        # Scale seconds to milliseconds rather than making the regression wait
        # a minute. Exercise the real stage timer and cancellation machinery.
        with patch.object(tasks,'BROWSER_STAGE_TIMEOUT_SECONDS',.03), \
                patch.object(tasks,'BROWSER_LOGIN_CHECK_TIMEOUT_SECONDS',.08,create=True):
            await tasks._run_browser_stage('login_check',asyncio.sleep(.05))
            with self.assertRaises(tasks.BrowserStageTimeout):
                await tasks._run_browser_stage('input_before_type',asyncio.sleep(.05),cancel_grace_seconds=.01)
            with self.assertRaises(tasks.BrowserStageTimeout) as caught:
                await tasks._run_browser_stage('login_check',asyncio.sleep(.2),cancel_grace_seconds=.01)
            self.assertEqual(caught.exception.stage,'login_check')

    async def test_visible_login_wall_still_stops_without_waiting_for_timeout(self):
        page=Mock()
        page.locator.return_value.first.count=AsyncMock(return_value=1)
        page.locator.return_value.first.is_visible=AsyncMock(return_value=True)
        with self.assertRaisesRegex(RuntimeError,'login_required'):
            await tasks._run_browser_stage('login_check',
                tasks.ensure_not_login_required(page,'synthetic','open_chat_page'))


if __name__=='__main__':unittest.main()
