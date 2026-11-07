"""Recovery uses one navigation path and retains fail-closed page checks."""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core import tasks


class FriendChatRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_navigates_once_without_preliminary_reload(self):
        page = Mock(goto=AsyncMock(), reload=AsyncMock())
        with patch.object(tasks, '_dismiss_non_login_dialogs', AsyncMock()) as dialogs, \
             patch.object(tasks, 'ensure_not_login_required', AsyncMock()) as login, \
             patch.object(tasks.asyncio, 'sleep', AsyncMock()):
            await tasks._reopen_friend_chat_page(page, 'synthetic')
        page.reload.assert_not_awaited()
        page.goto.assert_awaited_once_with(url=tasks.CREATOR_CHAT_URL)
        self.assertEqual(dialogs.await_args_list[0].args[2], 'before_reopen_chat_page')
        self.assertEqual(dialogs.await_args_list[-1].args[2], 'after_reopen_chat_page')
        login.assert_awaited_once_with(page, 'synthetic', 'reopen_chat_page')

    async def test_navigation_failure_is_bounded_and_does_not_refresh(self):
        page = Mock(goto=AsyncMock(side_effect=RuntimeError('synthetic navigation failure')),
                    reload=AsyncMock())
        with patch.object(tasks, '_dismiss_non_login_dialogs', AsyncMock()), \
             patch.object(tasks, 'ensure_not_login_required', AsyncMock()) as login, \
             patch.object(tasks.asyncio, 'sleep', AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'synthetic navigation failure'):
                await tasks._reopen_friend_chat_page(page, 'synthetic')
        self.assertEqual(page.goto.await_count, 2)
        page.reload.assert_not_awaited()
        login.assert_not_awaited()

    async def test_unknown_dialog_stops_before_navigation(self):
        page = Mock(goto=AsyncMock(), reload=AsyncMock())
        with patch.object(tasks, '_dismiss_non_login_dialogs',
                          AsyncMock(side_effect=tasks.BrowserDialogBlocked('synthetic dialog'))):
            with self.assertRaises(tasks.BrowserDialogBlocked):
                await tasks._reopen_friend_chat_page(page, 'synthetic')
        page.goto.assert_not_awaited()
        page.reload.assert_not_awaited()

    async def test_login_failure_after_navigation_is_not_swallowed(self):
        page = Mock(goto=AsyncMock(), reload=AsyncMock())
        with patch.object(tasks, '_dismiss_non_login_dialogs', AsyncMock()) as dialogs, \
             patch.object(tasks, 'ensure_not_login_required',
                          AsyncMock(side_effect=RuntimeError('synthetic login required'))), \
             patch.object(tasks.asyncio, 'sleep', AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'synthetic login required'):
                await tasks._reopen_friend_chat_page(page, 'synthetic')
        page.goto.assert_awaited_once()
        page.reload.assert_not_awaited()
        self.assertEqual(dialogs.await_count, 1)

    async def test_stage_timeout_is_propagated_without_extra_navigation(self):
        page = Mock(goto=AsyncMock(), reload=AsyncMock())
        async def hangs(**kwargs):
            import asyncio
            await asyncio.Future()
        page.goto.side_effect = hangs
        original = tasks._run_browser_stage
        with patch.object(tasks, '_dismiss_non_login_dialogs', AsyncMock()), \
             patch.object(tasks, '_run_browser_stage', wraps=tasks._run_browser_stage) as stage:
            async def short_stage(name, operation, **kwargs):
                kwargs['timeout_seconds'] = .02
                return await original(name, operation, **kwargs)
            stage.side_effect = short_stage
            with self.assertRaises(tasks.BrowserStageTimeout):
                await tasks._reopen_friend_chat_page(page, 'synthetic')
        page.goto.assert_awaited_once()
        page.reload.assert_not_awaited()
