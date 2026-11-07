"""Migration seams: final queued dispatch keeps Hangzhou fences and home windows."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch
from core import tasks


class HomeMigrationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_time_is_rechecked_inside_the_queued_enter_task(self):
        chat = Mock(press=AsyncMock())
        with patch.object(tasks.send_pause, 'should_pause_sending', return_value=False), \
             patch.object(tasks.personal_streak, 'authorized', return_value=True), \
             patch.object(tasks.personal_streak, 'within_send_window', return_value=False), \
             patch.object(tasks, '_browser_login_generation_current', return_value=True):
            allowed = await tasks._dispatch_browser_enter_if_current({'_personal_scope': {'claim': 'synthetic'}}, chat)
        self.assertFalse(allowed)
        chat.press.assert_not_awaited()

    async def test_global_and_manual_jobs_keep_existing_no_claim_semantics(self):
        chat = Mock(press=AsyncMock())
        with patch.object(tasks.send_pause, 'should_pause_sending', return_value=False), \
             patch.object(tasks.personal_streak, 'authorized', return_value=True), \
             patch.object(tasks, '_browser_login_generation_current', return_value=True):
            self.assertTrue(await tasks._dispatch_browser_enter_if_current({}, chat))
        chat.press.assert_awaited_once_with('Enter')
