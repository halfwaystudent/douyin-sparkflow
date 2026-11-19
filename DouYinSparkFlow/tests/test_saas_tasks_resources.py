import unittest
from core import tasks

class ResourceRoutingTests(unittest.TestCase):
    def test_resource_pressure_is_not_a_network_or_login_failure(self):
        class ResourcePressure(RuntimeError):
            category='browser_resource_unavailable'
        for stage in ['open_chat_page','send_flow','friend_list']:
            with self.subTest(stage=stage):
                self.assertEqual(tasks.classify_browser_failure(stage,ResourcePressure('insufficient memory')),'browser_resource_unavailable')
        self.assertIn('browser_resource_unavailable',tasks.ACCOUNT_LEVEL_FAILURE_CATEGORIES)

    def test_resource_pause_uses_existing_temporary_cooldown_not_whole_day(self):
        from datetime import datetime,timedelta,timezone
        from unittest.mock import patch
        from core import recovery_queue
        now=datetime(2026,11,19,4,tzinfo=timezone.utc)
        user={'account_failure':{'category':'browser_resource_unavailable','attemptCount':2,'lastAttemptAt':(now-timedelta(minutes=90)).isoformat()}}
        with patch.object(recovery_queue,'enabled',return_value=False):
            self.assertFalse(tasks._account_paused_by_failure_today(user,now))
            user['account_failure']['lastAttemptAt']=(now-timedelta(minutes=10)).isoformat()
            self.assertTrue(tasks._account_paused_by_failure_today(user,now))

    def test_low_session_memory_stops_before_checkpoint_without_recovery_probe(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from core.browser_resources import BrowserResourceUnavailable
        from tests.test_browser_stage_timeouts import flow
        with flow(identity_guard=False) as (user,page,context,browser,chat,release,run):
            with patch.object(tasks,'ensure_browser_resources',create=True,side_effect=BrowserResourceUnavailable({'check':'session'})), \
                 patch.object(tasks,'_mark_browser_target_in_flight') as mark, \
                 patch.object(tasks,'detect_message_already_sent',AsyncMock()) as recovery:
                asyncio.run(run())
            mark.assert_not_called();chat.press.assert_not_awaited();recovery.assert_not_awaited()
            self.assertEqual(user['account_failure']['category'],'browser_resource_unavailable')

if __name__=='__main__':unittest.main()
