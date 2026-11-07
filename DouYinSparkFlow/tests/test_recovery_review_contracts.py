"""Network-isolated contracts for recovery review findings F1--F4."""
import asyncio
from copy import deepcopy
from datetime import timedelta, timezone
import os
import unittest
from unittest.mock import AsyncMock, patch

from core import recovery_queue, tasks
from tests.test_automatic_recovery import CFG, NOW, recovery_store


class RecoveryReviewContracts(unittest.TestCase):
    def test_manual_selection_and_personal_run_do_not_project_recovery_queue(self):
        from core import automatic_recovery as recovery
        user = {'account_ref': 'a', 'username': 'a', 'targets': ['friend'], 'enabled': True}
        with patch.dict(os.environ, {'SPARKFLOW_MANUAL_RUN': '1', 'SPARKFLOW_PERSONAL_USER_ID': ''}, clear=False), \
                patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)), \
                patch.object(recovery, 'project_due', side_effect=AssertionError('manual must not project queue')):
            self.assertEqual(tasks._select_due_targets(user, CFG['dailySendWindow'], NOW)[0], ['friend'])

        with patch.dict(os.environ, {'SPARKFLOW_MANUAL_RUN': '1', 'SPARKFLOW_PERSONAL_USER_ID': ''}, clear=False), \
                patch.object(tasks, 'get_config', return_value=deepcopy(CFG)), \
                patch.object(tasks, 'get_userData', return_value=[]), \
                patch.object(recovery, 'run', new_callable=AsyncMock) as run:
            asyncio.run(tasks.runTasks())
            run.assert_not_awaited()

    def test_global_recovery_markers_route_to_single_target_engine(self):
        from core import automatic_recovery as recovery
        for marker in ('SPARKFLOW_RECOVERY_PASS', 'SPARKFLOW_RECONCILE_ONLY'):
            with self.subTest(marker=marker), \
                    patch.dict(os.environ, {'SPARKFLOW_MANUAL_RUN': '', 'SPARKFLOW_PERSONAL_USER_ID': '', marker: '1'}, clear=False), \
                    patch.object(tasks, 'get_config', return_value=deepcopy(CFG)), \
                    patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)), \
                    patch.object(recovery, 'run', new_callable=AsyncMock, return_value='recovered') as run:
                self.assertEqual(asyncio.run(tasks.runTasks()), 'recovered')
                run.assert_awaited_once()

    def test_manual_protocol_claim_remains_available_but_recovery_ticket_is_browser_only(self):
        from core import automatic_recovery as recovery, protocol_dispatch
        from tests.test_refusal_retry import account
        user = account()

        def update(fn, **_kwargs):
            result, _changed = fn([user])
            return result

        with patch.dict(os.environ, {'SPARKFLOW_MANUAL_RUN': '1', 'SPARKFLOW_RECOVERY_PASS': '', 'SPARKFLOW_RECONCILE_ONLY': ''}, clear=False), \
                patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)), \
                patch.object(protocol_dispatch, 'update_user_data', side_effect=update), \
                patch.object(protocol_dispatch, 'datetime') as clock, \
                patch.object(protocol_dispatch, 'get_userData', return_value=[user]):
            # This contract tests transport ownership inside the service window,
            # not whichever wall-clock hour the complete suite happens to run.
            clock.now.return_value = NOW
            self.assertEqual(protocol_dispatch._mark_protocol_targets_in_flight(user, 'manual'), ['friend'])
            user[recovery.TICKET] = {'target': 'friend', 'runId': 'recovery'}
            self.assertEqual(protocol_dispatch._mark_protocol_targets_in_flight(user, 'recovery'), [])

    def test_protocol_rechecks_quiet_hours_and_carries_service_deadline(self):
        from core import protocol_dispatch
        now = NOW.astimezone(timezone.utc).replace(hour=14, minute=0)
        user = {'username': 'a', 'targets': ['friend'],
                '_personal_scope': {'claim': 'owned'}}
        with patch.object(recovery_queue, 'allowed_now', return_value=False), \
                patch.object(protocol_dispatch, '_build_protocol_command', side_effect=AssertionError('quiet hour must not launch node')):
            self.assertEqual(protocol_dispatch._run_protocol_for_user(user, {}, False, {} )['runner'], 'recovery-window-closed')

        captured = {}
        class Completed:
            returncode = 0
            stdout = '{"ok": true, "sent": [], "resolved": [], "unresolved": []}'
            stderr = ''
        service_deadline = now.replace(hour=23, minute=50, second=0, microsecond=0)
        with patch.dict(os.environ, {'SPARKFLOW_PERSONAL_USER_ID': 'synthetic-owner'}), \
                patch.object(recovery_queue, 'allowed_now', return_value=True), \
                patch.object(recovery_queue, 'get_config', return_value=deepcopy(CFG)), \
                patch.object(recovery_queue, '_ctx', return_value=(timezone.utc, 'service', now, now.date().isoformat())), \
                patch('core.personal_streak.protocol_send_deadline', return_value=int((now + timedelta(days=1)).timestamp() * 1000)), \
                patch.object(protocol_dispatch, 'datetime') as clock, \
                patch.object(protocol_dispatch, '_build_protocol_command', return_value=(['synthetic-node'], '.', 'synthetic', '.', None)), \
                patch.object(protocol_dispatch.subprocess, 'run', side_effect=lambda *a, **kw: captured.update(payload=__import__('json').loads(kw['input'])) or Completed()):
            clock.now.return_value = now
            self.assertTrue(protocol_dispatch._run_protocol_for_user(user, {}, False, {})['ok'])
        self.assertEqual(captured['payload']['personalSendDeadlineEpochMs'], int(service_deadline.timestamp() * 1000))

    def test_fresh_login_or_identity_failure_blocks_claim_and_queued_enter(self):
        from core import automatic_recovery as recovery
        for flag in ('login_required', 'needs_relogin', 'pending_login_verification', 'identity_mismatch'):
            with self.subTest(flag=flag):
                user = {'account_ref': 'a', 'login_generation': 'g', 'username': 'a',
                        'unique_id': 'a', 'targets': ['friend'], 'enabled': True,
                        'cookies': [{'name': 'sessionid', 'value': 'synthetic', 'expires': -1}]}
                now = NOW.astimezone(timezone.utc).replace(hour=8)
                with recovery_store([user], now), patch.object(tasks, '_scheduled_send_time', return_value=now-timedelta(minutes=1)):
                    scoped = deepcopy(user)
                    scoped[recovery.TICKET] = {'day': now.date().isoformat(), 'target': 'friend', 'runId': flag}
                    self.assertTrue(tasks._mark_browser_target_in_flight(scoped, 'friend', run_id=flag))
                    disk = tasks.get_userData(force_reload=True)
                    disk[0][flag] = True
                    from utils import config as storage
                    # recovery_store binds tasks.update_user_data to this transaction.
                    tasks.update_user_data(lambda accounts: (accounts.__setitem__(0, disk[0]), True), force_reload=True)
                    self.assertFalse(recovery.dispatch_allowed(scoped, now))

    def test_default_window_ends_at_17_but_explicit_18_is_preserved(self):
        from utils.config import DEFAULT_CONFIG
        self.assertEqual(DEFAULT_CONFIG['dailySendWindow']['endHour'], 17)
        self.assertEqual(tasks._normalize_send_window({})['endHour'], 17)
        self.assertEqual(tasks._normalize_send_window({'dailySendWindow': {'endHour': 18}})['endHour'], 18)
