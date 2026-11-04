"""Synthetic, network-blocked dialog regressions; never loads an account."""
import unittest
from playwright.async_api import async_playwright
from core import tasks


class DialogSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.addAsyncCleanup(self.pw.stop)
        self.browser = await self.pw.chromium.launch(args=[
            '--no-sandbox', '--disable-dev-shm-usage',
            '--disable-background-networking', '--disable-component-update',
            '--host-resolver-rules=MAP * ~NOTFOUND',
        ])
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        self.addAsyncCleanup(self.context.close)
        self.requests = []
        async def deny(route):
            self.requests.append(route.request.url)
            await route.abort()
        await self.context.route('**/*', deny)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(1000)

    async def asyncTearDown(self):
        self.assertEqual([], self.requests)

    async def dialog(self, text='未知业务提示', button='确定', hidden=False):
        await self.page.set_content('''<script>window.clicked=0;</script>
          <div role="dialog" style="display:%s"><p>%s</p>
          <button onclick="window.clicked++;this.parentElement.remove()">%s</button>
          <button aria-label="Close" onclick="window.clicked++;this.parentElement.remove()">X</button></div>'''
          % ('none' if hidden else 'block', text, button))

    async def test_sender_never_confirms_an_unknown_dialog(self):
        await self.dialog()
        with self.assertRaisesRegex(RuntimeError, 'browser_dialog_blocked'):
            await tasks._dismiss_non_login_dialogs(self.page, 'synthetic', 'friend_list')
        self.assertEqual(0, await self.page.evaluate('window.clicked'))

    async def test_refresh_does_not_click_page_text_outside_a_dialog(self):
        from core import friends
        await self.page.set_content('''<script>window.clicked=0;</script>
          <p onclick="window.clicked++">我知道了</p>''')
        self.assertFalse(await friends._dismiss_non_login_dialogs(self.page))
        self.assertEqual(0, await self.page.evaluate('window.clicked'))

    async def test_hidden_dialogs_do_not_block_or_get_clicked(self):
        await self.dialog(hidden=True)
        self.assertEqual(0, await tasks._dismiss_non_login_dialogs(self.page, 'synthetic', 'friend_list'))
        self.assertEqual(0, await self.page.evaluate('window.clicked'))

    async def test_verification_dialog_is_never_confirmed(self):
        await self.dialog(text='安全验证')
        with self.assertRaisesRegex(RuntimeError, 'login_required'):
            await tasks._dismiss_non_login_dialogs(self.page, 'synthetic', 'friend_list')
        self.assertEqual(0, await self.page.evaluate('window.clicked'))

    async def test_refresh_unknown_dialog_is_scoped_and_has_sanitized_reason(self):
        from core import friends
        await self.dialog(text='synthetic-private-content')
        with self.assertRaises(friends.FriendRefreshError) as caught:
            await friends._dismiss_non_login_dialogs(self.page)
        self.assertEqual('browser_dialog_blocked', caught.exception.category)
        self.assertNotIn('synthetic-private-content', str(caught.exception))
        self.assertEqual(0, await self.page.evaluate('window.clicked'))


class DialogFailurePolicyTests(unittest.TestCase):
    def test_complete_friend_scan_cannot_clear_a_dialog_pause(self):
        from copy import deepcopy
        from unittest.mock import patch
        user = {'unique_id': 'synthetic', 'username': 'synthetic',
                'account_failure': {'category': 'browser_dialog_blocked', 'attemptCount': 1}}
        stored = deepcopy(user)
        original = deepcopy(user['account_failure'])
        def update(mutate, **kwargs):
            value, _changed = mutate([stored])
            return value
        with patch.object(tasks, 'update_user_data', side_effect=update):
            self.assertTrue(tasks._persist_friend_index(
                user, {'synthetic': {'visibleName': 'synthetic', 'stableKeys': []}},
                '2026-11-03T12:00:00+00:00', scan_complete=True))
        self.assertEqual(original, stored.get('account_failure'))
        self.assertEqual(original, user.get('account_failure'))

    def test_dialog_block_is_explained_without_calling_it_login_failure(self):
        from webui import ops
        from core import notifications
        for labels in (ops.FAILURE_CATEGORY_LABELS, notifications.CATEGORY_LABELS):
            self.assertIn('browser_dialog_blocked', labels)
            self.assertIn('弹窗', labels['browser_dialog_blocked'])
            self.assertNotIn('登录失效', labels['browser_dialog_blocked'])
        self.assertIn('browser_dialog_blocked', notifications.BROWSER_BATCH_FAILURES)

    def test_unknown_dialog_has_its_own_account_failure_category(self):
        category = tasks.classify_browser_failure(
            'friend_list', tasks.BrowserDialogBlocked('browser_dialog_blocked: review required'))
        self.assertEqual('browser_dialog_blocked', category)
        self.assertTrue(tasks._is_account_level_failure_category(category))

    def test_first_dialog_block_pauses_account_and_is_not_a_transport_retry(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        from webui import ops
        now = datetime.now(timezone.utc)
        failure = {'category': 'browser_dialog_blocked', 'attemptCount': 1,
                   'lastAttemptAt': now.isoformat()}
        with patch.dict('os.environ', {'SPARKFLOW_ACCOUNT_FAILURE_PAUSE_AFTER_ATTEMPTS': '99'}):
            self.assertTrue(tasks._account_paused_by_failure_today({'account_failure': failure}, now))
        self.assertTrue(ops._account_failure_pause_active(failure, now, 99))
        self.assertNotIn('browser_dialog_blocked', tasks.TEMPORARY_ACCOUNT_FAILURE_CATEGORIES)


class DialogRoutePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreadable_visible_dialog_is_blocked_without_clicking(self):
        from unittest.mock import AsyncMock, Mock
        page = Mock()
        item = page.locator.return_value.first
        item.count = AsyncMock(return_value=1)
        item.inner_text = AsyncMock(side_effect=RuntimeError('synthetic-private-content'))
        item.click = AsyncMock()
        with self.assertRaises(tasks.BrowserDialogBlocked) as caught:
            await tasks._dismiss_non_login_dialogs(page, 'synthetic', 'friend_list')
        item.click.assert_not_awaited()
        self.assertNotIn('synthetic-private-content', str(caught.exception))

    async def test_friend_refresh_does_not_try_another_route_for_a_dialog(self):
        from unittest.mock import AsyncMock, patch
        from core import friends
        error = friends.FriendRefreshError('review required', category='browser_dialog_blocked')
        attempt = AsyncMock(side_effect=error)
        with patch.object(friends, '_fetch_account_friends_once', attempt), \
             patch.object(friends, 'douyin_network_modes', return_value=('direct', 'mihomo')):
            with self.assertRaises(friends.FriendRefreshError):
                await friends.fetch_account_friends({'cookies': [{'name': 'synthetic', 'value': 'fake'}]})
        self.assertEqual(1, attempt.await_count)

    async def test_patrol_does_not_try_another_route_for_a_dialog(self):
        from unittest.mock import AsyncMock, patch
        from core import friends, patrol
        error = friends.FriendRefreshError('review required', category='browser_dialog_blocked')
        attempt = AsyncMock(side_effect=error)
        with patch.object(patrol, '_inspect_once', attempt), \
             patch.object(patrol, 'douyin_network_modes', return_value=('direct', 'mihomo')):
            with self.assertRaises(friends.FriendRefreshError):
                await patrol._inspect_account({'username': 'synthetic'}, {})
        self.assertEqual(1, attempt.await_count)
