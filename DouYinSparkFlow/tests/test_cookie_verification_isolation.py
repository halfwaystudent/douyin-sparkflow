"""Pasted credentials are checked without inheriting another caller's profile."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import login_desktop_server as desktop
from core import friends
from webui import app as app_module


COOKIES = [{"name": "sessionid", "value": "private-new-session", "domain": ".douyin.com", "path": "/"}]
TRUSTED = {"uid": "1234567890", "source": "creator_im_user_token", "version": 1}
PAGE_IDENTITY = {"unique_id": "public-handle", "username": "new account", "display_id": "public-handle"}


def browser_fixture():
    page = SimpleNamespace(
        goto=AsyncMock(), close=AsyncMock(), url=friends.CREATOR_HOME_URL,
    )
    context = SimpleNamespace(
        set_default_navigation_timeout=Mock(), set_default_timeout=Mock(),
        add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock(),
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    playwright = SimpleNamespace(stop=AsyncMock())
    return playwright, browser, context, page


class FreshCookieBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_pasted_cookies_skip_persistent_profile_but_keep_account_lock(self):
        runtime, browser, context, page = browser_fixture()
        account = {"cookies": deepcopy(COOKIES), "unique_id": "", "account_ref": "", "_fresh_login_verification": True}
        before = deepcopy(account)
        with patch.object(friends, "get_browser", new_callable=AsyncMock, return_value=(runtime, browser)), patch.object(friends, "get_persistent_browser_context", new_callable=AsyncMock) as persistent, patch.object(friends, "acquire_browser_account_lock", new_callable=AsyncMock, return_value=("guard", "lock-path", "token")) as lock, patch.object(friends, "release_browser_account_lock") as release, patch.object(friends, "get_config") as config, patch.object(friends, "collect_login_result", new_callable=AsyncMock, return_value=PAGE_IDENTITY), patch.object(friends, "_wait_for_chat_or_login", new_callable=AsyncMock):
            result = await friends._fetch_account_friends_once(account, "direct", auth_only=True)
        self.assertEqual(PAGE_IDENTITY, result)
        self.assertEqual(before, account)
        browser.new_context.assert_awaited_once_with(service_workers="block", accept_downloads=False)
        context.add_cookies.assert_awaited_once_with(COOKIES)
        persistent.assert_not_awaited()
        lock.assert_awaited_once()
        release.assert_called_once_with("guard", "lock-path", "token", "")
        config.assert_not_called()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        runtime.stop.assert_awaited_once()

    async def test_saved_account_background_check_keeps_its_profile_policy(self):
        runtime, browser, context, page = browser_fixture()
        policy = {"enabled": True, "root": "/private/profiles", "syncStoredCookiesBeforeRun": True, "seedCookiesWhenEmpty": True}
        account = {"cookies": deepcopy(COOKIES), "unique_id": "1234567890", "platform_uid": "1234567890"}
        with patch.object(friends, "get_config", return_value={}), patch.object(friends, "normalize_persistent_profile_config", return_value=policy), patch.object(friends, "get_browser", new_callable=AsyncMock) as ephemeral, patch.object(friends, "get_persistent_browser_context", new_callable=AsyncMock, return_value=(runtime, context, "/private/profiles/uid-1234567890")) as persistent, patch.object(friends, "acquire_browser_account_lock", new_callable=AsyncMock, return_value=("guard", "lock-path", "token")) as lock, patch.object(friends, "release_browser_account_lock") as release, patch("core.profile_cookies.prepare_profile_cookies", new_callable=AsyncMock, return_value=None) as prepare, patch.object(friends, "collect_login_result", new_callable=AsyncMock, return_value=PAGE_IDENTITY), patch.object(friends, "_wait_for_chat_or_login", new_callable=AsyncMock):
            await friends._fetch_account_friends_once(account, "direct", auth_only=True)
        ephemeral.assert_not_awaited()
        persistent.assert_awaited_once_with("uid-1234567890", root="/private/profiles", network_mode="direct")
        lock.assert_awaited_once()
        release.assert_called_once()
        prepare.assert_awaited_once_with(context, account, policy, "/private/profiles/uid-1234567890")
        context.add_cookies.assert_not_awaited()

    async def test_simultaneous_candidates_get_separate_contexts_and_only_their_cookies(self):
        first = browser_fixture()
        second = browser_fixture()
        other_cookies = [{**COOKIES[0], "value": "other-private-session"}]
        with patch.object(friends, "get_browser", new_callable=AsyncMock, side_effect=[first[:2], second[:2]]), patch.object(friends, "collect_login_result", new_callable=AsyncMock, return_value=PAGE_IDENTITY), patch.object(friends, "_wait_for_chat_or_login", new_callable=AsyncMock):
            await asyncio.gather(*[
                friends._fetch_account_friends_once({"cookies": cookies, "_fresh_login_verification": True}, "direct", auth_only=True)
                for cookies in (COOKIES, other_cookies)
            ])
        first[2].add_cookies.assert_awaited_once_with(COOKIES)
        second[2].add_cookies.assert_awaited_once_with(other_cookies)
        self.assertIsNot(first[2], second[2])

    async def test_cancelled_temporary_check_closes_all_browser_resources(self):
        runtime, browser, context, page = browser_fixture()
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()
        with patch.object(friends, "get_browser", new_callable=AsyncMock, return_value=(runtime, browser)), patch.object(friends, "collect_login_result", side_effect=stalled):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(friends._fetch_account_friends_once({"cookies": COOKIES, "_fresh_login_verification": True}, "direct", auth_only=True), timeout=0.02)
        page.close.assert_awaited_once()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        runtime.stop.assert_awaited_once()


class CookieVerificationDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def verify(self):
        return await app_module.verify_login_result({"cookies": deepcopy(COOKIES)})

    async def test_valid_cookie_uses_fresh_context_and_returns_trusted_uid(self):
        with patch.object(app_module, "verify_account_session", new_callable=AsyncMock, return_value={"identity": PAGE_IDENTITY}) as browser, patch.object(app_module.account_identity, "fetch_trusted_identity", return_value=TRUSTED):
            verified, reason, identity, category = await self.verify()
        self.assertTrue(verified)
        self.assertEqual("", reason)
        self.assertEqual("", category)
        self.assertEqual(TRUSTED["uid"], identity["unique_id"])
        self.assertEqual("public-handle", identity["display_id"])
        self.assertIs(True, browser.await_args.args[0]["_fresh_login_verification"])
        self.assertTrue(browser.await_args.kwargs["auth_only"])

    async def test_total_deadline_cancels_browser_and_reports_retryable_timeout(self):
        cancelled = asyncio.Event()
        async def stalled(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(app_module, "COOKIE_LOGIN_VERIFICATION_TIMEOUT_SECONDS", 0.01), patch.object(app_module, "verify_account_session", side_effect=stalled), patch.object(app_module.account_identity, "fetch_trusted_identity") as identity:
            verified, reason, info, category = await self.verify()
        self.assertFalse(verified)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(friends.CATEGORY_NETWORK_UNAVAILABLE, category)
        self.assertIn("超时", reason)
        self.assertEqual({}, info["trusted"])
        identity.assert_not_called()

    async def test_deadline_includes_identity_phase(self):
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()
        with patch.object(app_module, "COOKIE_LOGIN_VERIFICATION_TIMEOUT_SECONDS", 0.01), patch.object(app_module, "verify_account_session", new_callable=AsyncMock, return_value={"identity": PAGE_IDENTITY}), patch.object(app_module.asyncio, "to_thread", side_effect=stalled):
            verified, reason, info, category = await self.verify()
        self.assertFalse(verified)
        self.assertEqual(friends.CATEGORY_NETWORK_UNAVAILABLE, category)
        self.assertEqual({}, info["trusted"])

    async def test_transport_and_unknown_failures_do_not_claim_cookie_expired(self):
        for error, category in ((OSError("private transport detail"), friends.CATEGORY_NETWORK_UNAVAILABLE), (RuntimeError("private page detail"), "verification_error")):
            with self.subTest(error=type(error).__name__), patch.object(app_module, "verify_account_session", new_callable=AsyncMock, side_effect=error):
                verified, reason, info, actual = await self.verify()
            self.assertFalse(verified)
            self.assertEqual(category, actual)
            self.assertNotIn("private", reason)
            self.assertEqual({}, info["trusted"])

    async def test_explicit_login_and_network_categories_are_preserved(self):
        for category in (friends.CATEGORY_LOGIN_REQUIRED, friends.CATEGORY_NETWORK_UNAVAILABLE, friends.CATEGORY_STRUCTURE_CHANGED):
            with self.subTest(category=category), patch.object(app_module, "verify_account_session", new_callable=AsyncMock, side_effect=friends.FriendRefreshError("classified failure", category=category)):
                verified, reason, info, actual = await self.verify()
            self.assertFalse(verified)
            self.assertEqual(category, actual)

    async def test_unavailable_identity_does_not_create_a_login_verdict(self):
        with patch.object(app_module, "verify_account_session", new_callable=AsyncMock, return_value={"identity": PAGE_IDENTITY}), patch.object(app_module.account_identity, "fetch_trusted_identity", side_effect=app_module.account_identity.IdentityUnavailable("identity temporarily unavailable")):
            verified, reason, info, category = await self.verify()
        self.assertFalse(verified)
        self.assertEqual(app_module.account_identity.CATEGORY_IDENTITY_UNVERIFIED, category)

    async def test_request_cancellation_remains_cancellation(self):
        with patch.object(app_module, "verify_account_session", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.verify()


class ExportTimerOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_starting_next_workspace_cancels_previous_export_stop(self):
        manager = desktop.LoginDesktopManager()
        manager.context = SimpleNamespace(_impl_obj=object())
        manager.stop = AsyncMock()
        with patch.object(desktop, "STOP_AFTER_EXPORT_SECONDS", 0.01):
            manager.schedule_stop_after_export()
            old_timer = manager._scheduled_stop_task
            await manager.start()
            await asyncio.sleep(0.02)
        self.assertTrue(old_timer.cancelled())
        self.assertIsNone(manager._scheduled_stop_task)
        manager.stop.assert_not_awaited()
