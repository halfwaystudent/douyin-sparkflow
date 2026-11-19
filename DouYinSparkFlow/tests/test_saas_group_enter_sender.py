"""Actual sender integration; no browser, account or external network."""
import asyncio
from contextlib import ExitStack,contextmanager
from datetime import datetime,timezone
import unittest
from unittest.mock import AsyncMock,Mock,patch
from core import tasks,saas_group_guard,streak_state
from tests.test_browser_stage_timeouts import flow

TARGET='群聊：群甲'
BOUND='7691234567890123401'
NOW=datetime(2026,11,19,4,tzinfo=timezone.utc)

@contextmanager
def group_flow(*,arm_ok=True,dispatch_ok=True):
    with flow(identity_guard=False) as values,ExitStack() as stack:
        user,page,context,browser,chat,release,run=values
        user['targets']=[TARGET];user['group_target_bindings']={TARGET:BOUND}
        clock=stack.enter_context(patch.object(tasks,'datetime',wraps=datetime))
        clock.now.side_effect=lambda tz=None:NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
        stack.enter_context(patch.object(tasks.im_ui,'is_saas_ui',AsyncMock(return_value=True)))
        stack.enter_context(patch.object(tasks,'_verify_open_group_conversation',AsyncMock(return_value=(True,''))))
        arm=stack.enter_context(patch.object(saas_group_guard,'arm_enter',AsyncMock(return_value=(arm_ok,'blocked' if not arm_ok else '')),create=True))
        consume=stack.enter_context(patch.object(saas_group_guard,'dispatch_was_allowed',AsyncMock(return_value=dispatch_ok),create=True))
        disarm=stack.enter_context(patch.object(saas_group_guard,'disarm',AsyncMock(),create=True))
        recovery=stack.enter_context(patch.object(tasks,'detect_message_already_sent',AsyncMock(return_value=(False,'offline'))))
        yield values,arm,consume,disarm,recovery

class GroupEnterSenderTests(unittest.TestCase):
    def test_failed_group_arm_stops_before_enter_and_keeps_unknown(self):
        with group_flow(arm_ok=False) as (values,arm,consume,disarm,recovery):
            user,page,context,browser,chat,release,run=values
            user['targets'].append('second')
            asyncio.run(run())
            chat.press.assert_not_awaited();recovery.assert_not_awaited()
            arm.assert_awaited_once_with(page,chat,BOUND)
            self.assertTrue(streak_state.is_sent_unverified(user,TARGET,NOW))
            self.assertEqual(user['account_failure']['category'],'group_identity_unavailable')
            disarm.assert_awaited_once_with(page)

    def test_saas_editor_pins_guard_even_if_shell_disappears_before_arm(self):
        with group_flow(arm_ok=False) as (values,arm,consume,disarm,recovery):
            user,page,context,browser,chat,release,run=values
            with patch.object(tasks.im_ui,'is_saas_ui',AsyncMock(return_value=False)),patch.object(tasks,'locate_chat_input',AsyncMock(return_value=(chat,'#sub-app [data-e2e="msg-input"] [contenteditable="true"]:visible'))):
                asyncio.run(run())
            chat.press.assert_not_awaited()
            arm.assert_awaited_once_with(page,chat,BOUND)

    def test_group_arm_after_barrier_before_gate_and_consume_before_confirm(self):
        with group_flow() as (values,arm,consume,disarm,recovery):
            user,page,context,browser,chat,release,run=values
            events=[];original=tasks._persist_browser_pre_enter_unknown
            def barrier(*args,**kw):events.append('barrier');return original(*args,**kw)
            async def armed(*args):events.append('arm');return True,''
            async def gate():events.append('gate');return {}
            async def pressed(key):events.append('enter')
            async def consumed(*args):events.append('consume');return True
            async def confirm(*args,**kw):events.append('confirm');return True,'offline'
            arm.side_effect=armed;consume.side_effect=consumed;chat.press.side_effect=pressed
            report_gate=Mock(ensure_before_enter=AsyncMock(side_effect=gate),snapshot=lambda:{})
            with patch.object(tasks,'_persist_browser_pre_enter_unknown',side_effect=barrier),patch.object(tasks.action_report_gate,'attach',return_value=report_gate),patch.object(tasks,'confirm_message_sent',AsyncMock(side_effect=confirm)):
                asyncio.run(run())
            self.assertEqual(events,['barrier','arm','gate','enter','consume','confirm'])
            disarm.assert_awaited_once_with(page)

    def test_blocked_dispatch_does_not_confirm_recover_or_advance_target(self):
        with group_flow(dispatch_ok=False) as (values,arm,consume,disarm,recovery):
            user,page,context,browser,chat,release,run=values
            user['targets'].append('second')
            with patch.object(tasks,'confirm_message_sent',AsyncMock(return_value=(True,'offline'))) as confirm:
                asyncio.run(run())
            self.assertEqual(chat.press.await_count,1)
            confirm.assert_not_awaited();recovery.assert_not_awaited()
            self.assertTrue(streak_state.is_sent_unverified(user,TARGET,NOW))
            self.assertEqual(user['account_failure']['category'],'group_identity_unavailable')
            disarm.assert_awaited_once_with(page)

if __name__=='__main__':unittest.main()
