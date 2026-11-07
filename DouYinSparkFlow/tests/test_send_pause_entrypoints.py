"""Offline checks for today's pause at real dispatch entry points."""
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
from core import tasks,protocol_dispatch,send_pause

class SendPauseEntrypointTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_blocks_scheduler_before_configuration_or_account_read(self):
        with patch.object(send_pause,'should_pause_sending',return_value=True), patch.object(tasks,'get_config',side_effect=RuntimeError('must not read config')) as cfg:
            try:
                result=await tasks.runTasks()
            except RuntimeError:
                result=None
            cfg.assert_not_called()
            self.assertEqual('skipped_sending_paused',result)

    async def test_pause_blocks_manual_browser_before_network_selection(self):
        with patch.object(send_pause,'should_pause_sending',return_value=True), patch.object(tasks,'_run_browser_tasks',new_callable=AsyncMock) as run:
            await tasks.run_browser_tasks({},[{'_personal_scope':{}}])
            run.assert_not_awaited()

    async def test_pause_blocks_enter_inside_queued_dispatch(self):
        chat=Mock(press=AsyncMock())
        with patch.object(send_pause,'should_pause_sending',return_value=True), patch.object(tasks,'_browser_login_generation_current',return_value=True), patch.object(tasks.personal_streak,'authorized',return_value=True):
            allowed=await tasks._dispatch_browser_enter_if_current({},chat)
        self.assertFalse(allowed)
        chat.press.assert_not_awaited()

    async def test_permission_revoked_while_queued_blocks_enter(self):
        chat=Mock(press=AsyncMock())
        with patch.object(send_pause,'should_pause_sending',return_value=False), patch.object(tasks,'_browser_login_generation_current',return_value=True), patch.object(tasks.personal_streak,'authorized',return_value=False):
            allowed=await tasks._dispatch_browser_enter_if_current({},chat)
        self.assertFalse(allowed)
        chat.press.assert_not_awaited()

    async def test_unpaused_authorized_current_generation_can_enter_once(self):
        chat=Mock(press=AsyncMock())
        with patch.object(send_pause,'should_pause_sending',return_value=False), patch.object(tasks,'_browser_login_generation_current',return_value=True), patch.object(tasks.personal_streak,'authorized',return_value=True):
            allowed=await tasks._dispatch_browser_enter_if_current({},chat)
        self.assertTrue(allowed)
        chat.press.assert_awaited_once_with('Enter')

    async def test_pause_blocks_protocol_entry_before_preparation(self):
        with patch.object(send_pause,'should_pause_sending',return_value=True), patch.object(protocol_dispatch,'_normalize_send_strategy',return_value={}) as prepare:
            result=await protocol_dispatch.run_protocol_tasks({},[],Mock())
        self.assertEqual([],result)
        prepare.assert_not_called()

    async def test_pause_blocks_protocol_worker_before_subprocess_dispatch(self):
        with patch.object(send_pause,'should_pause_sending',return_value=True), patch.object(protocol_dispatch,'_build_protocol_command',return_value=(['fake-node'],Path('.'),'node','.',None)), patch.object(protocol_dispatch.subprocess,'run',return_value=Mock(stdout='{"ok":true,"sent":[]}',stderr='',returncode=0)) as run:
            result=protocol_dispatch._run_protocol_for_user({}, {},False,{})
        run.assert_not_called()
        self.assertEqual('sending-paused',result['runner'])

if __name__=='__main__':unittest.main()
