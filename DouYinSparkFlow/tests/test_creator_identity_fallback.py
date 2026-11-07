import asyncio
import unittest
from unittest.mock import patch

from core import friends
from tests.test_account_login_multi_method import (
    ACCOUNT_ID, _FakeBrowser, _FakeContext, _FakePage, _FakePlaywright,
    force_ephemeral_profiles, trusted,
)


class BrowserContext(_FakeContext):
    async def cookies(self, *args, **kwargs):
        return [{"name": "sessionid", "value": "browser-session"}]


class BoundIdentityFallbackTests(unittest.TestCase):
    def setUp(self):
        force_ephemeral_profiles(self)
        self.account = {
            "username": "Saved nickname", "platform_uid": ACCOUNT_ID,
            "identity_binding": trusted(), "display_id": "saved-handle",
            "cookies": [{"name": "sessionid", "value": "stored-session"}],
        }

    def run_check(self, *, page=None, identity=None, identity_error=None,
                  card_error=None, bound=True, chat_error=None):
        page = page or _FakePage()
        context = BrowserContext(page)
        browser = _FakeBrowser(context)
        runtime = _FakePlaywright()
        self.chat_steps = []

        async def get_browser(*args, **kwargs):
            return runtime, browser

        async def card(*args, **kwargs):
            raise RuntimeError(card_error or friends.CARD_NOT_READY_MESSAGE)

        def read_identity(cookies):
            self.assertEqual(friends.CHAT_PAGE_URL, page.url)
            self.assertEqual("browser-session", cookies[0]["value"])
            if identity_error:
                raise identity_error
            return identity or trusted()

        async def click_friends(page):
            self.chat_steps.append("friends-tab")

        async def friend_rows(page):
            self.chat_steps.append("friend-list")
            if chat_error:
                raise chat_error
            return "", None

        account = dict(self.account)
        if not bound:
            account.pop("identity_binding")
        with (
            patch.object(friends, "get_browser", side_effect=get_browser),
            patch.object(friends, "collect_login_result", side_effect=card),
            patch.object(friends, "fetch_trusted_identity", side_effect=read_identity) as probe,
            patch.object(friends, "_click_friends_tab", side_effect=click_friends),
            patch.object(friends, "_wait_for_friend_rows_or_empty", side_effect=friend_rows),
            patch.object(friends, "IDENTITY_READ_ATTEMPTS", 1),
            patch.object(friends._wait_for_chat_or_login, "__defaults__", (0.01,)),
        ):
            try:
                result = asyncio.run(friends._fetch_account_friends_once(
                    account, "direct", auth_only=True,
                ))
                return result, page, probe.call_count
            finally:
                self.assertTrue(browser.closed)
                self.assertTrue(context.closed)
                self.assertTrue(runtime.stopped)

    def test_missing_home_card_uses_bound_browser_identity_after_chat_is_ready(self):
        result, page, calls = self.run_check()
        self.assertEqual(ACCOUNT_ID, result["unique_id"])
        self.assertEqual("Saved nickname", result["username"])
        self.assertEqual("saved-handle", result["display_id"])
        self.assertEqual([friends.CREATOR_HOME_URL, friends.CHAT_PAGE_URL], page.visited)
        self.assertEqual(1, calls)
        self.assertEqual(["friends-tab", "friend-list"], self.chat_steps)

    def test_another_browser_identity_is_rejected(self):
        with self.assertRaises(friends.FriendRefreshError) as caught:
            self.run_check(identity=trusted("999999"))
        self.assertEqual(friends.CATEGORY_IDENTITY_CONFLICT, caught.exception.category)
        self.assertEqual([], self.chat_steps)

    def test_unavailable_identity_endpoint_does_not_verify_the_account(self):
        with self.assertRaises(friends.FriendRefreshError) as caught:
            self.run_check(identity_error=friends.IdentityUnavailable("身份接口不可用"))
        self.assertEqual(friends.CATEGORY_IDENTITY_UNVERIFIED, caught.exception.category)

    def test_missing_chat_page_still_fails(self):
        with self.assertRaises(friends.FriendRefreshError) as caught:
            self.run_check(page=_FakePage(sub_app_count=0))
        self.assertEqual(friends.CATEGORY_STRUCTURE_CHANGED, caught.exception.category)

    def test_chat_shell_without_readable_friends_is_not_accepted(self):
        with self.assertRaises(friends.FriendRefreshError) as caught:
            self.run_check(chat_error=friends.FriendRefreshError("friend list did not become ready"))
        self.assertEqual(friends.CATEGORY_STRUCTURE_CHANGED, caught.exception.category)

    def test_redirected_chat_page_is_not_accepted(self):
        class RedirectedChat(_FakePage):
            async def goto(self, url, **kwargs):
                await super().goto(url, **kwargs)
                if url == friends.CHAT_PAGE_URL:
                    self.url = "https://www.douyin.com/"

        with self.assertRaises(friends.FriendRefreshError):
            self.run_check(page=RedirectedChat())

    def test_visible_chat_login_mask_is_not_accepted(self):
        class LoginMask:
            first = property(lambda self: self)

            async def count(self):
                return 1

            async def is_visible(self):
                return True

        class LoginPage(_FakePage):
            def locator(self, selector):
                if selector == friends.LOGIN_MASK_SELECTORS[0]:
                    return LoginMask()
                return super().locator(selector)

        with self.assertRaises(friends.FriendRefreshError) as caught:
            self.run_check(page=LoginPage())
        self.assertEqual(friends.CATEGORY_LOGIN_REQUIRED, caught.exception.category)

    def test_unbound_legacy_account_keeps_the_existing_identity_checks(self):
        with self.assertRaisesRegex(RuntimeError, friends.CARD_NOT_READY_MESSAGE):
            self.run_check(bound=False)

    def test_other_home_errors_do_not_trigger_the_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "net::ERR_CONNECTION_RESET"):
            self.run_check(card_error="net::ERR_CONNECTION_RESET")

    def test_visible_home_login_form_does_not_trigger_the_fallback(self):
        with self.assertRaises(friends.FriendRefreshError) as caught:
            self.run_check(card_error="账号登录已失效，请重新扫码登录")
        self.assertEqual(friends.CATEGORY_LOGIN_REQUIRED, caught.exception.category)


if __name__ == "__main__":
    unittest.main()
