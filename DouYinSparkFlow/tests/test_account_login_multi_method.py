import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import login_desktop_server
from core import cookies as cookie_module
from core import friends as friends_module
from core import login as login_module
from core import streak_state
from webui import app as app_module
from webui import login_lock

ACCOUNT_ID = "1234567890"


def isolate_account_data(test_case, path):
    """Point the account store at ``path`` whatever the ambient environment is.

    ``get_userData()`` switches its data source when ``GITHUB_ACTIONS`` is set: it
    reads the ``USER_DATA`` secret instead of the JSON file. A test that only
    patches ``users_data_path`` therefore sees an empty account list on CI (and
    the 404s that follow) while passing on a machine that happens to have local
    account data. Pin the environment to LOCAL and drop the module-level cache so
    these tests behave the same everywhere.
    """
    from utils import config as config_module

    env_patch = patch.object(
        config_module, "get_environment", return_value=config_module.Environment.LOCAL
    )
    env_patch.start()
    test_case.addCleanup(env_patch.stop)

    cached = config_module.userData
    config_module.userData = None
    test_case.addCleanup(lambda: setattr(config_module, "userData", cached))

    path_patch = patch.object(config_module, "users_data_path", return_value=path)
    path_patch.start()
    test_case.addCleanup(path_patch.stop)



def _json_export():
    return json.dumps(
        [
            {
                "domain": ".douyin.com",
                "name": "sessionid",
                "value": "abc123",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "expirationDate": 4102444800,
            },
            {"name": "sid_guard", "value": "def456"},
        ]
    )


class CookieInputParsingTests(unittest.TestCase):
    def test_json_array_export_is_normalized(self):
        cookies = cookie_module.parse_cookie_input(_json_export())

        self.assertEqual(2, len(cookies))
        self.assertEqual("sessionid", cookies[0]["name"])
        self.assertEqual(".douyin.com", cookies[0]["domain"])
        self.assertEqual("/", cookies[0]["path"])
        self.assertTrue(cookies[0]["httpOnly"])
        self.assertAlmostEqual(4102444800.0, cookies[0]["expires"])

    def test_header_string_gains_domain_and_path(self):
        cookies = cookie_module.parse_cookie_input("Cookie: sessionid=abc; sid_guard=def")

        self.assertEqual(["sessionid", "sid_guard"], [item["name"] for item in cookies])
        self.assertEqual({".douyin.com"}, {item["domain"] for item in cookies})
        self.assertEqual({"/"}, {item["path"] for item in cookies})

    def test_single_cookie_value_is_accepted(self):
        cookies = cookie_module.parse_cookie_input("sessionid=only-value")

        self.assertEqual(1, len(cookies))
        self.assertEqual("only-value", cookies[0]["value"])

    def test_domain_gains_leading_dot_and_empty_path_defaults(self):
        cookies = cookie_module.parse_cookie_input(
            json.dumps([{"name": "sessionid", "value": "v", "domain": "creator.douyin.com", "path": ""}])
        )

        self.assertEqual(".creator.douyin.com", cookies[0]["domain"])
        self.assertEqual("/", cookies[0]["path"])

    def test_unparseable_input_is_rejected(self):
        with self.assertRaises(cookie_module.CookieParseError):
            cookie_module.parse_cookie_input("this is not a cookie")

        with self.assertRaises(cookie_module.CookieParseError):
            cookie_module.parse_cookie_input("")

        with self.assertRaises(cookie_module.CookieParseError):
            cookie_module.parse_cookie_input(json.dumps([]))

    def test_missing_auth_cookie_is_rejected(self):
        cookies = cookie_module.parse_cookie_input("ttwid=no-auth-here")

        self.assertEqual([], cookie_module.auth_cookie_names(cookies))
        with self.assertRaises(cookie_module.CookieParseError) as caught:
            cookie_module.require_auth_cookies(cookies)
        self.assertEqual("cookie_auth_missing", caught.exception.category)

    def test_cookie_summary_never_contains_values(self):
        cookies = cookie_module.parse_cookie_input(_json_export())

        summary = cookie_module.cookie_summary(cookies)
        rendered = json.dumps(summary)

        self.assertEqual(2, summary["count"])
        self.assertNotIn("abc123", rendered)
        self.assertNotIn("def456", rendered)


class FriendRefreshCategoryTests(unittest.TestCase):
    def test_login_failures_are_classified_as_login_required(self):
        self.assertEqual(
            friends_module.CATEGORY_LOGIN_REQUIRED,
            friends_module.classify_refresh_error(RuntimeError("账号登录已失效，请重新扫码登录")),
        )

    def test_transport_failures_are_classified_as_network(self):
        self.assertEqual(
            friends_module.CATEGORY_NETWORK_UNAVAILABLE,
            friends_module.classify_refresh_error(RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")),
        )

    def test_other_failures_are_classified_as_structure_change(self):
        self.assertEqual(
            friends_module.CATEGORY_STRUCTURE_CHANGED,
            friends_module.classify_refresh_error(
                RuntimeError("好友列表已加载但未找到可读取的好友行")
            ),
        )

    def test_missing_dom_is_structure_change_not_network(self):
        # A loaded page with missing DOM used to be reported as a network failure
        # because the message contains the word "timeout".
        for message in (
            "friend list did not become ready within timeout; dom={}",
            "chat page did not load within timeout",
        ):
            self.assertEqual(
                friends_module.CATEGORY_STRUCTURE_CHANGED,
                friends_module.classify_refresh_error(RuntimeError(message)),
                message,
            )

    def test_categories_attached_by_raisers_survive_classification(self):
        error = friends_module.FriendRefreshError(
            "friend list did not become ready within timeout",
            category=friends_module.CATEGORY_LOGIN_REQUIRED,
        )

        self.assertEqual(
            friends_module.CATEGORY_LOGIN_REQUIRED,
            friends_module.classify_refresh_error(error),
        )


class _FakeLocator:
    def __init__(self, count):
        self._count = count

    async def count(self):
        return self._count


class _FakePage:
    """Minimal page double that records navigation and reports #sub-app."""

    def __init__(self, *, sub_app_count=1, url="https://creator.douyin.com/", redirect_to=None):
        self.url = url
        self.sub_app_count = sub_app_count
        self.redirect_to = redirect_to
        self.visited = []
        self.reload_timeouts = []

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        self.url = self.redirect_to or url

    async def reload(self, **kwargs):
        self.reload_timeouts.append(kwargs.get("timeout"))

    def locator(self, selector):
        if selector == "#sub-app":
            return _FakeLocator(self.sub_app_count)
        return _FakeLocator(0)

    async def wait_for_selector(self, selector, timeout=None):
        return None

    async def close(self):
        return None


class _FakeContext:
    def __init__(self, page):
        self._page = page
        self.added_cookies = []
        self.closed = False

    async def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)

    async def new_page(self):
        return self._page

    async def cookies(self, *args, **kwargs):
        return [{"name": "sessionid", "value": "v"}]

    def set_default_navigation_timeout(self, value):
        return None

    def set_default_timeout(self, value):
        return None

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, context):
        self._context = context
        self.closed = False

    async def new_context(self):
        return self._context

    async def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


class AuthOnlyFetchTests(unittest.TestCase):
    """Exercise the real auth_only branch instead of stubbing it out."""

    def _run(self, page, identity):
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright()

        async def fake_get_browser(*args, **kwargs):
            return playwright, browser

        async def fake_login_result(page_arg, context_arg, timeout_ms=300000):
            return identity

        with (
            patch.object(friends_module, "get_browser", side_effect=fake_get_browser),
            patch.object(friends_module, "collect_login_result", side_effect=fake_login_result),
        ):
            result = asyncio.run(
                friends_module._fetch_account_friends_once(
                    {"cookies": [{"name": "sessionid", "value": "v"}]},
                    "direct",
                    auth_only=True,
                )
            )
        return result, page, context, browser, playwright

    def test_auth_only_visits_creator_home_then_chat_and_returns_identity(self):
        page = _FakePage(sub_app_count=1)

        result, page, context, browser, playwright = self._run(
            page,
            {"unique_id": f" {ACCOUNT_ID} ", "username": " Tester "},
        )

        self.assertEqual(ACCOUNT_ID, result["unique_id"])
        self.assertEqual("Tester", result["username"])
        self.assertEqual(
            [friends_module.CREATOR_HOME_URL, friends_module.CHAT_PAGE_URL],
            page.visited,
        )
        self.assertTrue(browser.closed)
        self.assertTrue(context.closed)
        self.assertTrue(playwright.stopped)

    def test_auth_only_without_identity_on_creator_host_is_not_login_required(self):
        # Still on the creator host: a slow or changed page must not be reported
        # as a bad Cookie, so other routes can still be tried.
        page = _FakePage(sub_app_count=1, url="https://creator.douyin.com/login")

        async def failing_login_result(page_arg, context_arg, timeout_ms=300000):
            raise RuntimeError("net::ERR_TIMED_OUT waiting for READY_SELECTOR")

        context = _FakeContext(page)
        browser = _FakeBrowser(context)

        async def fake_get_browser(*args, **kwargs):
            return _FakePlaywright(), browser

        with (
            patch.object(friends_module, "get_browser", side_effect=fake_get_browser),
            patch.object(friends_module, "collect_login_result", side_effect=failing_login_result),
            self.assertRaises(RuntimeError) as caught,
        ):
            asyncio.run(
                friends_module._fetch_account_friends_once(
                    {"cookies": [{"name": "sessionid", "value": "v"}]},
                    "direct",
                    auth_only=True,
                )
            )

        self.assertEqual(
            friends_module.CATEGORY_NETWORK_UNAVAILABLE,
            caught.exception.category,
        )

    def test_auth_only_redirected_away_from_creator_is_login_required(self):
        # The creator home URL bounces to the public site, which proves the
        # Cookie is not usable.
        page = _FakePage(sub_app_count=1, redirect_to="https://www.douyin.com/")

        async def failing_login_result(page_arg, context_arg, timeout_ms=300000):
            raise RuntimeError("READY_SELECTOR not found")

        context = _FakeContext(page)
        browser = _FakeBrowser(context)

        async def fake_get_browser(*args, **kwargs):
            return _FakePlaywright(), browser

        with (
            patch.object(friends_module, "get_browser", side_effect=fake_get_browser),
            patch.object(friends_module, "collect_login_result", side_effect=failing_login_result),
            self.assertRaises(friends_module.FriendRefreshError) as caught,
        ):
            asyncio.run(
                friends_module._fetch_account_friends_once(
                    {"cookies": [{"name": "sessionid", "value": "v"}]},
                    "direct",
                    auth_only=True,
                )
            )

        self.assertEqual(
            friends_module.CATEGORY_LOGIN_REQUIRED,
            caught.exception.category,
        )

    def test_auth_only_without_chat_page_is_structure_change(self):
        # Identity resolves but the friend page never exposes #sub-app.
        page = _FakePage(sub_app_count=0)

        async def fake_login_result(page_arg, context_arg, timeout_ms=300000):
            return {"unique_id": ACCOUNT_ID, "username": "Tester"}

        context = _FakeContext(page)
        browser = _FakeBrowser(context)

        async def fake_get_browser(*args, **kwargs):
            return _FakePlaywright(), browser

        with (
            patch.object(friends_module, "get_browser", side_effect=fake_get_browser),
            patch.object(friends_module, "collect_login_result", side_effect=fake_login_result),
            patch.object(
                friends_module._wait_for_chat_or_login,
                "__defaults__",
                (0.2,),
            ),
            self.assertRaises(friends_module.FriendRefreshError) as caught,
        ):
            asyncio.run(
                friends_module._fetch_account_friends_once(
                    {"cookies": [{"name": "sessionid", "value": "v"}]},
                    "direct",
                    auth_only=True,
                )
            )

        self.assertEqual(
            friends_module.CATEGORY_STRUCTURE_CHANGED,
            caught.exception.category,
        )

    def test_chat_wait_accepts_a_missing_timeout(self):
        page = _FakePage(sub_app_count=1)

        # Must not raise: a None timeout previously produced a TypeError that was
        # misreported as a network failure.
        with patch.object(
            friends_module._wait_for_chat_or_login,
            "__defaults__",
            (0.2,),
        ):
            asyncio.run(friends_module._wait_for_chat_or_login(page, timeout_seconds=None))


class StoredSessionVerificationTests(unittest.TestCase):
    def test_missing_cookies_raise_login_required(self):
        with self.assertRaises(friends_module.FriendRefreshError) as caught:
            asyncio.run(friends_module.verify_account_session({"cookies": []}))

        self.assertEqual(
            friends_module.CATEGORY_LOGIN_REQUIRED,
            caught.exception.category,
        )

    def test_unreachable_routes_raise_network_category(self):
        async def boom(account, network_mode, **kwargs):
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")
        with (
            patch.object(friends_module, "douyin_network_modes", return_value=["direct"]),
            patch.object(friends_module, "_fetch_account_friends_once", side_effect=boom),
            self.assertRaises(friends_module.FriendRefreshError) as caught,
        ):
            asyncio.run(
                friends_module.verify_account_session({"cookies": [{"name": "sessionid", "value": "v"}]})
            )

        self.assertEqual(
            friends_module.CATEGORY_NETWORK_UNAVAILABLE,
            caught.exception.category,
        )

    def test_verified_session_returns_identity(self):
        async def ok(account, network_mode, **kwargs):
            return {"unique_id": ACCOUNT_ID, "username": "Tester"}

        with (
            patch.object(friends_module, "douyin_network_modes", return_value=["direct"]),
            patch.object(friends_module, "_fetch_account_friends_once", side_effect=ok) as fetch,
        ):
            result = asyncio.run(
                friends_module.verify_account_session(
                    {"cookies": [{"name": "sessionid", "value": "v"}]},
                    auth_only=True,
                )
            )

        self.assertTrue(result["verified"])
        self.assertEqual(ACCOUNT_ID, result["identity"]["unique_id"])
        self.assertTrue(fetch.call_args.kwargs.get("auth_only"))

    def test_verification_raises_login_required_when_page_shows_login_mask(self):
        async def login_required(account, network_mode, **kwargs):
            raise RuntimeError("账号登录已失效，请重新扫码登录")

        with (
            patch.object(friends_module, "douyin_network_modes", return_value=["direct"]),
            patch.object(friends_module, "_fetch_account_friends_once", side_effect=login_required),
            self.assertRaises(friends_module.FriendRefreshError) as caught,
        ):
            asyncio.run(
                friends_module.verify_account_session({"cookies": [{"name": "sessionid", "value": "v"}]})
            )

        self.assertEqual(
            friends_module.CATEGORY_LOGIN_REQUIRED,
            caught.exception.category,
        )

    def test_structure_failure_does_not_consume_the_second_route(self):
        # The identity read raises a plain RuntimeError that carries its category
        # as an attribute, so this must cover that shape and not only FriendRefreshError.
        calls = []
        failure = friends_module._with_category(
            RuntimeError(login_module.CARD_NOT_READY_MESSAGE),
            friends_module.CATEGORY_STRUCTURE_CHANGED,
        )

        async def structural(account, network_mode, **kwargs):
            calls.append(network_mode)
            raise failure

        with (
            patch.object(friends_module, "douyin_network_modes", return_value=["direct", "mihomo"]),
            patch.object(friends_module, "_fetch_account_friends_once", side_effect=structural),
            self.assertRaises(friends_module.FriendRefreshError) as caught,
        ):
            asyncio.run(
                friends_module.verify_account_session({"cookies": [{"name": "sessionid", "value": "v"}]})
            )

        self.assertEqual(["direct"], calls)
        self.assertEqual(
            friends_module.CATEGORY_STRUCTURE_CHANGED,
            caught.exception.category,
        )

    def test_chat_page_structure_failure_also_stops_at_the_first_route(self):
        calls = []

        async def structural(account, network_mode, **kwargs):
            calls.append(network_mode)
            raise friends_module.FriendRefreshError(
                "chat page did not load within timeout",
                category=friends_module.CATEGORY_STRUCTURE_CHANGED,
            )

        with (
            patch.object(friends_module, "douyin_network_modes", return_value=["direct", "mihomo"]),
            patch.object(friends_module, "_fetch_account_friends_once", side_effect=structural),
            self.assertRaises(friends_module.FriendRefreshError) as caught,
        ):
            asyncio.run(
                friends_module.verify_account_session({"cookies": [{"name": "sessionid", "value": "v"}]})
            )

        self.assertEqual(["direct"], calls)
        self.assertEqual(
            friends_module.CATEGORY_STRUCTURE_CHANGED,
            caught.exception.category,
        )

    def test_unrecognised_error_still_tries_the_next_route(self):
        # classify_refresh_error defaults unknown text to the structure category;
        # an unknown failure must not lose the remaining egress routes because of it.
        calls = []

        async def unknown(account, network_mode, **kwargs):
            calls.append(network_mode)
            raise RuntimeError("unexpected verification failure")

        with (
            patch.object(friends_module, "douyin_network_modes", return_value=["direct", "mihomo"]),
            patch.object(friends_module, "_fetch_account_friends_once", side_effect=unknown),
            self.assertRaises(friends_module.FriendRefreshError) as caught,
        ):
            asyncio.run(
                friends_module.verify_account_session({"cookies": [{"name": "sessionid", "value": "v"}]})
            )

        self.assertEqual(["direct", "mihomo"], calls)
        self.assertEqual(
            friends_module.CATEGORY_STRUCTURE_CHANGED,
            caught.exception.category,
        )

    def test_network_failure_still_tries_the_next_route(self):
        calls = []

        async def refused(account, network_mode, **kwargs):
            calls.append(network_mode)
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")

        with (
            patch.object(friends_module, "douyin_network_modes", return_value=["direct", "mihomo"]),
            patch.object(friends_module, "_fetch_account_friends_once", side_effect=refused),
            self.assertRaises(friends_module.FriendRefreshError) as caught,
        ):
            asyncio.run(
                friends_module.verify_account_session({"cookies": [{"name": "sessionid", "value": "v"}]})
            )

        self.assertEqual(["direct", "mihomo"], calls)
        self.assertEqual(
            friends_module.CATEGORY_NETWORK_UNAVAILABLE,
            caught.exception.category,
        )


class SavedLoginHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        from pathlib import Path

        self.users_path = Path(self.temp_dir.name) / "usersData.json"
        self.users_path.write_text("[]", encoding="utf-8")
        isolate_account_data(self, self.users_path)

    def test_unverified_login_keeps_failure_markers(self):
        account, action = app_module.save_exported_login_result(
            {
                "unique_id": ACCOUNT_ID,
                "username": "Tester",
                "cookies": [{"name": "sessionid", "value": "v", "domain": ".douyin.com", "path": "/"}],
            },
            is_healthy=False,
            verification_reason="登录态仍不可用",
        )

        self.assertEqual("created", action)
        self.assertTrue(account["pending_login_verification"])
        self.assertTrue(account["login_required"])
        self.assertFalse(account["account_health"]["healthy"])

        preflight = streak_state.preflight_account(account)
        self.assertFalse(preflight["healthy"])
        self.assertEqual("login_verification_pending", preflight["category"])

    def test_verified_login_clears_failure_markers(self):
        app_module.save_exported_login_result(
            {
                "unique_id": ACCOUNT_ID,
                "username": "Tester",
                "cookies": [{"name": "sessionid", "value": "v", "domain": ".douyin.com", "path": "/"}],
            },
            is_healthy=False,
            verification_reason="登录态仍不可用",
        )

        account, action = app_module.save_exported_login_result(
            {
                "unique_id": ACCOUNT_ID,
                "username": "Tester",
                "cookies": [{"name": "sessionid", "value": "fresh", "domain": ".douyin.com", "path": "/"}],
            },
            is_healthy=True,
        )

        self.assertEqual("updated", action)
        self.assertNotIn("pending_login_verification", account)
        self.assertNotIn("login_required", account)
        self.assertNotIn("account_health", account)
        self.assertTrue(streak_state.preflight_account(account)["healthy"])

    def test_relogin_identity_mismatch_is_recorded(self):
        app_module.save_exported_login_result(
            {
                "unique_id": ACCOUNT_ID,
                "username": "Tester",
                "cookies": [{"name": "sessionid", "value": "v", "domain": ".douyin.com", "path": "/"}],
            },
            is_healthy=True,
        )

        account, _ = app_module.save_exported_login_result(
            {
                "unique_id": "9999999999",
                "username": "Other",
                "cookies": [{"name": "sessionid", "value": "w", "domain": ".douyin.com", "path": "/"}],
            },
            relogin_unique_id=ACCOUNT_ID,
            is_healthy=True,
        )

        self.assertEqual("9999999999", account["unique_id"])
        self.assertEqual(ACCOUNT_ID, account["identity_mismatch"]["expected"])
        self.assertEqual("9999999999", account["identity_mismatch"]["actual"])


class WebCookieLoginEndpointTests(unittest.TestCase):
    def setUp(self):
        try:
            login_lock.LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        self.client = TestClient(app_module.app)
        self.principal = {
            "username": "alice",
            "role": "user",
            "account_refs": [],
            "session_id": "session-1",
        }

    def test_unauthorized_cookie_login_is_rejected(self):
        response = self.client.post("/accounts/cookies", data={"cookie_input": "sessionid=abc"})

        self.assertEqual(401, response.status_code)

    def test_clear_state_requires_the_workspace_lease_for_normal_users(self):
        # Non-admins must own the shared login workspace even when no lease is
        # currently active, so they cannot fight another session for the browser.
        with (
            patch.object(app_module, "current_user", return_value="alice"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "get_login_lock", return_value=None),
            patch.object(app_module, "call_login_desktop") as call_login,
        ):
            response = self.client.post("/login-desktop/clear-state", data={"csrf_token": "t"})

        self.assertEqual(423, response.status_code)
        call_login.assert_not_called()

    def test_unparseable_cookie_input_is_rejected_without_touching_accounts(self):
        with (
            patch.object(app_module, "current_user", return_value="alice"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "update_user_data") as update,
        ):
            response = self.client.post(
                "/accounts/cookies",
                data={"csrf_token": "t", "cookie_input": "not-a-cookie"},
            )

        self.assertEqual(400, response.status_code)
        self.assertEqual("cookie_format_invalid", response.json()["category"])
        update.assert_not_called()

    def test_failed_verification_does_not_create_or_modify_accounts(self):
        with (
            patch.object(app_module, "current_user", return_value="alice"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(
                app_module,
                "verify_login_result",
                return_value=(
                    False,
                    "登录态在真实页面中不可用",
                    {"unique_id": "", "username": ""},
                    friends_module.CATEGORY_LOGIN_REQUIRED,
                ),
            ),
            patch.object(app_module, "update_user_data") as update,
        ):
            response = self.client.post(
                "/accounts/cookies",
                data={"csrf_token": "t", "cookie_input": "sessionid=abc; sid_guard=def"},
            )

        body = response.json()
        self.assertEqual(400, response.status_code)
        self.assertEqual("login_required", body["category"])
        self.assertFalse(body["retryable"])
        update.assert_not_called()

    def test_transport_verification_failure_keeps_its_category(self):
        # A network problem during verification must not be presented as a bad
        # Cookie, and it stays retryable.
        with (
            patch.object(app_module, "current_user", return_value="alice"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(
                app_module,
                "verify_login_result",
                return_value=(
                    False,
                    "无法读取好友私信页，无法验证登录态：net::ERR_PROXY_CONNECTION_FAILED",
                    {"unique_id": "", "username": ""},
                    friends_module.CATEGORY_NETWORK_UNAVAILABLE,
                ),
            ),
            patch.object(app_module, "update_user_data") as update,
        ):
            response = self.client.post(
                "/accounts/cookies",
                data={"csrf_token": "t", "cookie_input": "sessionid=abc; sid_guard=def"},
            )

        body = response.json()
        self.assertEqual(400, response.status_code)
        self.assertEqual("network_unavailable", body["category"])
        self.assertTrue(body["retryable"])
        update.assert_not_called()

    def test_verified_cookie_login_reports_created_account_without_leaking_cookie(self):
        saved = {
            "account_ref": "acc-1",
            "unique_id": ACCOUNT_ID,
            "username": "Tester",
            "enabled": True,
        }
        with (
            patch.object(app_module, "current_user", return_value="alice"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(
                app_module,
                "verify_login_result",
                return_value=(True, "", {"unique_id": ACCOUNT_ID, "username": "Tester"}, ""),
            ),
            patch.object(app_module, "account_by_unique_id", return_value=None),
            patch.object(app_module, "save_exported_login_result", return_value=(saved, "created")) as save,
            patch.object(app_module, "update_web_user") as update_user,
            patch.object(app_module, "call_login_desktop", return_value={"ok": True}) as clear_state,
        ):
            response = self.client.post(
                "/accounts/cookies",
                data={"csrf_token": "t", "cookie_input": "sessionid=secret-value; sid_guard=def"},
            )

        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertTrue(body["ok"])
        self.assertEqual("created", body["action"])
        self.assertNotIn("secret-value", response.text)
        update_user.assert_called_once()
        clear_state.assert_called_once()
        self.assertEqual("/clear-login-state", clear_state.call_args.args[0])
        login_result = save.call_args.args[0]
        self.assertEqual("pasted", login_result["cookie_source"])
        self.assertEqual(ACCOUNT_ID, login_result["unique_id"])


class FriendRefreshEndpointTests(unittest.TestCase):
    def setUp(self):
        try:
            login_lock.LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        import json as json_module
        from pathlib import Path

        self.temp_dir = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.users_path = Path(self.temp_dir.name) / "usersData.json"
        self.principal = {
            "username": "alice",
            "role": "admin",
            "account_refs": [],
            "session_id": "session-1",
        }
        self.account = {
            "account_ref": "acc-1",
            "unique_id": ACCOUNT_ID,
            "username": "Tester",
            "cookies": [{"name": "sessionid", "value": "v"}],
            "friends_cache": ["Old Friend"],
            "friends_cache_updated_at": "2026-01-01T00:00:00",
        }
        self.users_path.write_text(
            json_module.dumps([self.account], ensure_ascii=False),
            encoding="utf-8",
        )
        isolate_account_data(self, self.users_path)
        self.client = TestClient(app_module.app)
        self.addCleanup(lambda: app_module._friend_refresh_active.discard(ACCOUNT_ID))

    def _stored_account(self):
        import json as json_module

        accounts = json_module.loads(self.users_path.read_text(encoding="utf-8"))
        return accounts[0] if accounts else {}

    def _post(self):
        return self.client.post(
            f"/accounts/{ACCOUNT_ID}/friends/refresh",
            data={"csrf_token": "t"},
        )

    def _run_refresh(self, fetch):
        """Drive the real route and data layer; only the browser fetch is mocked."""
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            fetch as fetch_mock,
        ):
            response = self._post()
        return response, fetch_mock

    def test_busy_account_returns_conflict_and_touches_nothing(self):
        app_module._friend_refresh_active.add(ACCOUNT_ID)

        response, fetch = self._run_refresh(
            patch.object(app_module, "fetch_account_friends"),
        )

        # The acceptance criterion requires 429 or 423 for a concurrent refresh.
        self.assertIn(response.status_code, {429, 423})
        self.assertEqual(429, response.status_code)
        self.assertEqual("5", response.headers["retry-after"])
        self.assertTrue(response.json()["retryable"])
        fetch.assert_not_called()
        self.assertEqual(["Old Friend"], self._stored_account()["friends_cache"])

    def test_login_failure_keeps_previous_friends_cache(self):
        def fake_fetch(account):
            raise friends_module.FriendRefreshError(
                "账号登录已失效，请重新扫码登录",
                category=friends_module.CATEGORY_LOGIN_REQUIRED,
            )

        response, _ = self._run_refresh(
            patch.object(app_module, "fetch_account_friends", side_effect=fake_fetch),
        )

        body = response.json()
        self.assertEqual(401, response.status_code)
        self.assertEqual("login_required", body["category"])
        self.assertFalse(body["retryable"])
        self.assertEqual(self.account["friends_cache_updated_at"], body["previousUpdatedAt"])
        stored = self._stored_account()
        self.assertEqual(["Old Friend"], stored["friends_cache"])
        self.assertEqual("2026-01-01T00:00:00", stored["friends_cache_updated_at"])

    def test_network_failure_is_reported_as_retryable_and_keeps_cache(self):
        def fake_fetch(account):
            raise friends_module.FriendRefreshError(
                "无法连接抖音",
                category=friends_module.CATEGORY_NETWORK_UNAVAILABLE,
            )

        response, _ = self._run_refresh(
            patch.object(app_module, "fetch_account_friends", side_effect=fake_fetch),
        )

        body = response.json()
        self.assertEqual(502, response.status_code)
        self.assertEqual("network_unavailable", body["category"])
        self.assertTrue(body["retryable"])
        self.assertEqual(["Old Friend"], self._stored_account()["friends_cache"])

    def test_empty_result_keeps_previous_friends_cache(self):
        self.assertEqual(["Old Friend"], self.account["friends_cache"])

        def fake_fetch(account):
            del account
            return friends_module.FriendScanResult()

        response, _ = self._run_refresh(
            patch.object(app_module, "fetch_account_friends", side_effect=fake_fetch),
        )

        body = response.json()
        self.assertEqual(502, response.status_code)
        self.assertEqual("empty_result", body["category"])
        self.assertTrue(body["retryable"])
        self.assertEqual(self.account["friends_cache_updated_at"], body["previousUpdatedAt"])
        stored = self._stored_account()
        self.assertEqual(["Old Friend"], stored["friends_cache"])
        self.assertEqual("2026-01-01T00:00:00", stored["friends_cache_updated_at"])

    def test_complete_scan_updates_cache_and_reports_index(self):
        def fake_fetch(account):
            del account
            return friends_module.FriendScanResult(["Alice", "Bob"], complete=True)

        response, _ = self._run_refresh(
            patch.object(app_module, "fetch_account_friends", side_effect=fake_fetch),
        )

        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertTrue(body["scan_complete"])
        self.assertIsInstance(body["index_updated"], bool)
        stored = self._stored_account()
        self.assertEqual(["Alice", "Bob"], stored["friends_cache"])
        self.assertNotEqual("2026-01-01T00:00:00", stored["friends_cache_updated_at"])

    def test_structure_failure_keeps_previous_cache(self):
        def fake_fetch(account):
            raise friends_module.FriendRefreshError(
                "好友列表已加载但未找到可读取的好友行",
                category=friends_module.CATEGORY_STRUCTURE_CHANGED,
            )

        response, _ = self._run_refresh(
            patch.object(app_module, "fetch_account_friends", side_effect=fake_fetch),
        )

        body = response.json()
        self.assertEqual(502, response.status_code)
        self.assertEqual("structure_changed", body["category"])
        self.assertEqual(["Old Friend"], self._stored_account()["friends_cache"])

    def test_failure_responses_carry_previous_updated_at_for_the_ui(self):
        # The dashboard keeps the previous successful refresh time visible after a
        # failure, so every failure branch must report it.
        def fake_fetch(account):
            raise friends_module.FriendRefreshError(
                "无法连接抖音",
                category=friends_module.CATEGORY_NETWORK_UNAVAILABLE,
            )

        response, _ = self._run_refresh(
            patch.object(app_module, "fetch_account_friends", side_effect=fake_fetch),
        )

        body = response.json()
        self.assertEqual("2026-01-01T00:00:00", body["previousUpdatedAt"])
        self.assertEqual("网络不可用", body["categoryLabel"])

    def test_timeout_keeps_previous_cache(self):
        async def slow_fetch(account):
            raise asyncio.TimeoutError()

        async def fake_wait_for(awaitable, timeout):
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise asyncio.TimeoutError()

        with patch.object(app_module.asyncio, "wait_for", side_effect=fake_wait_for):
            response, _ = self._run_refresh(
                patch.object(app_module, "fetch_account_friends", side_effect=slow_fetch),
            )

        body = response.json()
        self.assertEqual(504, response.status_code)
        self.assertEqual("network_unavailable", body["category"])
        self.assertEqual(["Old Friend"], self._stored_account()["friends_cache"])

    def test_successful_refresh_writes_cache(self):
        response, _ = self._run_refresh(
            patch.object(app_module, "fetch_account_friends", return_value=["Alice", "Bob"]),
        )

        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertEqual(["Alice", "Bob"], body["friends"])
        self.assertEqual(self.account["friends_cache_updated_at"], body["previous_updated_at"])
        stored = self._stored_account()
        self.assertEqual(["Alice", "Bob"], stored["friends_cache"])
        self.assertNotEqual("2026-01-01T00:00:00", stored["friends_cache_updated_at"])
        self.assertEqual([], list(app_module._friend_refresh_active))


class LoginDesktopQrPayloadTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(login_desktop_server.app)

    def test_qr_reports_not_ready_with_retry_after(self):
        class Candidate:
            def __init__(self):
                self.count_value = 0

            async def count(self):
                return 0

        class FakePage:
            url = "https://creator.douyin.com/"

            def locator(self, selector):
                return Candidate()

        async def fake_page():
            return FakePage()

        async def not_logged_in(page, *, probe_timeout_ms=800):
            return {"state": "login_form", "logged_in": False, "url": page.url}

        with (
            patch.object(login_desktop_server.manager, "_get_active_page", side_effect=fake_page),
            patch.object(login_desktop_server.manager, "_login_page_state", side_effect=not_logged_in),
        ):
            response = self.client.get("/qr")

        body = response.json()
        self.assertEqual(202, response.status_code)
        self.assertEqual("qr_not_ready", body["state"])
        self.assertEqual(2, body["retry_after"])
        self.assertTrue(body["retryable"])

    def test_qr_reports_busy_when_page_lock_is_held(self):
        with patch.object(
            login_desktop_server.manager._page_operation_lock,
            "locked",
            return_value=True,
        ):
            response = self.client.get("/qr")

        body = response.json()
        self.assertEqual(503, response.status_code)
        self.assertEqual("qr_page_busy", body["state"])
        self.assertIn("重试", body["message"])
        self.assertTrue(body["retryable"])
        self.assertNotIn("creator", body["message"])

    def test_qr_reports_logged_in_state_without_waiting(self):
        class Candidate:
            async def count(self):
                return 0

        class FakePage:
            url = "https://creator.douyin.com/"

            def locator(self, selector):
                return Candidate()

        async def fake_page():
            return FakePage()

        async def logged_in(page, *, probe_timeout_ms=800):
            return {"state": "logged_in", "logged_in": True, "url": page.url}

        with (
            patch.object(login_desktop_server.manager, "_get_active_page", side_effect=fake_page),
            patch.object(login_desktop_server.manager, "_login_page_state", side_effect=logged_in),
        ):
            response = self.client.get("/qr")

        body = response.json()
        # 202 keeps the logged-in signal retryable through the WebUI proxy, which
        # forwards JSON instead of wrapping non-202 responses as an image.
        self.assertEqual(202, response.status_code)
        self.assertEqual("qr_logged_in", body["state"])
        self.assertTrue(body["logged_in"])
        self.assertTrue(body["retryable"])
        self.assertIn("重置", body["message"])

    def test_qr_expired_is_not_retryable(self):
        class Candidate:
            async def count(self):
                return 1

            class _First:
                async def is_visible(self):
                    return True

            @property
            def first(self):
                return self._First()

        class FakePage:
            url = "https://creator.douyin.com/"

            def locator(self, selector):
                return Candidate()

        async def fake_page():
            return FakePage()

        with patch.object(login_desktop_server.manager, "_get_active_page", side_effect=fake_page):
            response = self.client.get("/qr")

        body = response.json()
        self.assertEqual(409, response.status_code)
        self.assertEqual("qr_expired", body["state"])
        self.assertFalse(body["retryable"])
        self.assertIn("刷新二维码", body["message"])

    def test_clear_login_state_endpoint_reports_cleared_cookies(self):
        async def cleared():
            return {"ok": True, "cleared": 3, "url": "https://creator.douyin.com/"}

        with patch.object(login_desktop_server.manager, "clear_login_state", side_effect=cleared):
            response = self.client.post("/clear-login-state")

        self.assertEqual(200, response.status_code)
        self.assertEqual(3, response.json()["cleared"])

    def test_open_login_reports_busy_instead_of_internal_error(self):
        async def busy():
            raise RuntimeError("login page is busy; retry shortly")

        with patch.object(login_desktop_server.manager, "open_login", side_effect=busy):
            response = self.client.post("/open-login")

        body = response.json()
        self.assertEqual(503, response.status_code)
        self.assertEqual("qr_page_busy", body["state"])
        self.assertTrue(body["retryable"])


class _FakeUpstream:
    def __init__(self, body, *, status=200, content_type="application/json", retry_after=None):
        self._body = body
        self.status = status
        headers = {"Content-Type": content_type}
        if retry_after:
            headers["Retry-After"] = str(retry_after)
        self.headers = headers

    def read(self):
        return self._body

    def close(self):
        return None


class DashboardRefreshStatusTests(unittest.TestCase):
    """A3: the last successful refresh time must survive a failed refresh."""

    def setUp(self):
        self.script = (Path(app_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")

    def test_failure_path_keeps_the_last_successful_refresh_time(self):
        self.assertIn("上次成功刷新", self.script)
        self.assertIn("showRefreshOutcome", self.script)
        # The failure branch must render the retained timestamp, not only a message.
        failure_block = self.script[
            self.script.index("showRefreshOutcome(`刷新失败：") :
            self.script.index("showRefreshOutcome(`刷新失败：") + 200
        ]
        self.assertIn("showRefreshOutcome", failure_block)
        self.assertIn("lastSuccessAt = data.previousUpdatedAt", self.script)
        self.assertIn("data.categoryLabel", self.script)

    def test_success_path_records_the_new_refresh_time(self):
        # The refresh is now an async job the panel polls, so the successful
        # timestamp comes from the finished job rather than the POST response.
        self.assertIn("/async", self.script)
        self.assertIn("${refreshUrl}/status", self.script)
        self.assertIn("lastSuccessAt = job.updatedAt", self.script)

    def test_refresh_reports_collection_progress(self):
        self.assertIn("正在读取好友列表…已采集", self.script)
        self.assertIn("data-refresh-all-friends", self.script)


class DuplicateAccountGuardTests(unittest.TestCase):
    """A login save must not silently add a second row for the same person."""

    def test_same_name_candidates_are_matched_by_trimmed_nickname(self):
        accounts = [
            {"account_ref": "acc-1", "username": "Tester", "unique_id": "1"},
            {"account_ref": "acc-2", "username": "Other", "unique_id": "2"},
            {"account_ref": "acc-3", "username": "  Tester ", "unique_id": "3"},
            {"account_ref": "acc-4", "username": "", "unique_id": "4"},
        ]
        matches = app_module.find_same_name_account(accounts, "Tester")
        self.assertEqual(["acc-1", "acc-3"], [item["account_ref"] for item in matches])
        self.assertEqual([], app_module.find_same_name_account(accounts, "   "))
        self.assertEqual([], app_module.find_same_name_account(accounts, "Nobody"))
        self.assertEqual([], app_module.find_same_name_account(None, "Tester"))

    def test_console_flags_shared_nicknames(self):
        from webui import ops

        accounts = [
            {"username": "Tester"},
            {"username": "Tester"},
            {"username": "Other"},
            {"username": ""},
        ]
        self.assertEqual({"Tester": 2}, ops.duplicate_display_names(accounts))
        self.assertEqual({}, ops.duplicate_display_names([{"username": "Solo"}]))
        self.assertEqual({}, ops.duplicate_display_names(None))


class MergeDuplicateAccountTests(unittest.TestCase):
    """Merging a duplicate row must carry its targets and ledgers over."""

    def _merge(self, accounts):
        import json as json_module
        import tempfile
        from pathlib import Path as PathClass

        from utils import config

        with tempfile.TemporaryDirectory() as tmp:
            path = PathClass(tmp) / "usersData.json"
            path.write_text(json_module.dumps(accounts, ensure_ascii=False), encoding="utf-8")
            merged, changed = config.merge_user_account_into("2", "1", path=path)
            stored = json_module.loads(path.read_text(encoding="utf-8"))
        return merged, changed, stored

    def test_merge_carries_targets_and_ledgers_then_removes_source(self):
        accounts = [
            {
                "unique_id": "1",
                "username": "Same",
                "account_ref": "acc-keep",
                "enabled": False,
                "targets": ["A"],
                "target_states": {"A": {"done": True}},
                "friend_index": {"A": {"stableKeys": ["k1"]}},
            },
            {
                "unique_id": "2",
                "username": "Same",
                "account_ref": "acc-drop",
                "enabled": True,
                "targets": ["B"],
                "target_states": {"B": {"done": False}},
                "friend_index": {"B": {"stableKeys": ["k2"]}},
                "cookies": [{"name": "sid"}],
            },
        ]
        merged, changed, stored = self._merge(accounts)

        self.assertTrue(changed)
        self.assertEqual("1", merged["unique_id"])
        self.assertEqual(["A", "B"], merged["targets"])
        self.assertEqual({"A", "B"}, set(merged["target_states"]))
        self.assertEqual({"A", "B"}, set(merged["friend_index"]))
        # The source row is gone and no other row was touched.
        self.assertEqual(["1"], [item["unique_id"] for item in stored])
        # Identity fields stay the kept account's: cookies never move across.
        self.assertNotIn("cookies", stored[0])
        # Enabled is the union so a merge cannot silently disable sending.
        self.assertTrue(stored[0]["enabled"])

    def test_merge_keeps_the_kept_rows_value_on_a_conflicting_key(self):
        accounts = [
            {
                "unique_id": "1",
                "username": "Same",
                "targets": ["A"],
                "target_states": {"A": {"owner": "kept"}},
                "target_refs": {"A": {"ref": "kept"}},
            },
            {
                "unique_id": "2",
                "username": "Same",
                "targets": ["A"],
                "target_states": {"A": {"owner": "source"}},
                "target_refs": {"A": {"ref": "source"}},
                "friend_index_meta": {"A": {"scanned": 2}},
                "message_history": {"A": [2]},
            },
        ]
        merged, changed, stored = self._merge(accounts)

        self.assertTrue(changed)
        # A conflicting key must resolve in the kept row's favour. Switching
        # setdefault to update would silently overwrite it and this test fails.
        self.assertEqual({"owner": "kept"}, merged["target_states"]["A"])
        self.assertEqual({"ref": "kept"}, merged["target_refs"]["A"])
        # Keys the kept row lacks are still carried over, including the maps the
        # other test never touches.
        self.assertEqual({"scanned": 2}, merged["friend_index_meta"]["A"])
        self.assertEqual([2], merged["message_history"]["A"])
        self.assertEqual(["A"], merged["targets"])
        self.assertEqual(["1"], [item["unique_id"] for item in stored])

    def test_merge_refuses_self_and_missing_accounts(self):
        from utils import config

        with self.assertRaises(ValueError):
            config.merge_user_account_into("1", "1")
        _, changed, stored = self._merge(
            [{"unique_id": "1", "username": "Same", "targets": []}]
        )
        self.assertFalse(changed)
        self.assertEqual(1, len(stored))


class ReloginOverwriteTests(unittest.TestCase):
    """Re-logging into a chosen account must overwrite it, not be refused."""

    def test_overwrite_keeps_targets_and_ledger_and_adds_no_row(self):
        accounts = [
            {
                "unique_id": "123",
                "username": "Old",
                "account_ref": "acc-1",
                "targets": ["A", "B"],
                "target_states": {"A": {"done": True}},
                "cookies": [{"name": "old", "value": "1"}],
            }
        ]

        def fake_update(mutator, **_kwargs):
            return mutator(accounts)

        with patch.object(app_module, "update_user_data", side_effect=fake_update):
            account, action = app_module.save_exported_login_result(
                {
                    "unique_id": "999",
                    "username": "New",
                    "cookies": [
                        {"name": "sessionid", "value": "x"},
                        {"name": "sid_guard", "value": "y"},
                    ],
                },
                relogin_account_ref="acc-1",
                relogin_unique_id="123",
            )

        # Identity and login state are overwritten...
        self.assertEqual("999", account["unique_id"])
        self.assertEqual("New", account["username"])
        # ...while the operator's targets and send ledger survive, and no second
        # row is created for the same person.
        self.assertEqual(["A", "B"], account["targets"])
        self.assertEqual({"A": {"done": True}}, account["target_states"])
        self.assertEqual(1, len(accounts))
        self.assertEqual("updated", action)

    def test_differing_uid_is_accepted_so_the_account_is_overwritten(self):
        import asyncio
        from unittest.mock import AsyncMock

        login_result = {
            "unique_id": "999",
            "username": "Tester",
            "cookies": [
                {"name": "sessionid", "value": "x"},
                {"name": "sid_guard", "value": "y"},
            ],
        }
        with patch.object(
            app_module,
            "verify_account_session",
            new=AsyncMock(
                return_value={"identity": {"unique_id": "999", "username": "Tester"}}
            ),
        ):
            verified, reason, identity, category = asyncio.run(
                app_module.verify_login_result(
                    login_result,
                    relogin_account_ref="acc-1",
                    relogin_unique_id="123",
                )
            )

        # Refusing here is what used to leave a duplicate row behind, so a
        # differing id must come back as a successful verification.
        self.assertTrue(verified)
        self.assertEqual("", reason)
        self.assertEqual("", category)
        self.assertEqual("999", identity["unique_id"])


class ScheduleWindowLockTests(unittest.TestCase):
    """The send window may only be edited from outside a running window."""

    def setUp(self):
        self.client = TestClient(app_module.app)
        self.principal = {
            "username": "admin",
            "role": "admin",
            "account_refs": [],
            "session_id": "session-1",
        }

    def test_save_is_refused_while_the_window_is_running(self):
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(
                app_module,
                "schedule_window_state",
                return_value={
                    "enabled": True,
                    "inside": True,
                    "startHour": 10,
                    "endHour": 18,
                    "label": "10:00-18:00/20m",
                },
            ),
            patch.object(app_module, "update_daily_schedule") as update,
        ):
            response = self.client.post(
                "/ops/schedule",
                data={"csrf_token": "t", "daily_schedule": "08:00-09:00"},
                follow_redirects=False,
            )

        # Refused, and crucially nothing was written: no partial config change and
        # no cron rewrite while the window is running.
        self.assertEqual(303, response.status_code)
        update.assert_not_called()


class MergeRedirectContractTests(unittest.TestCase):
    """The merge button is a plain form, so the route must redirect, not return JSON."""

    def setUp(self):
        self.client = TestClient(app_module.app)
        self.principal = {
            "username": "admin",
            "role": "admin",
            "account_refs": [],
            "session_id": "session-1",
        }

    def _post_merge(self):
        source = {"unique_id": ACCOUNT_ID, "username": "Same", "account_ref": "acc-1"}
        target = {"unique_id": "999", "username": "Other", "account_ref": "acc-2"}
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(
                app_module, "ensure_account_refs", side_effect=lambda accounts: (accounts, False)
            ),
            patch.object(app_module, "get_userData", return_value=[source, target]),
            patch.object(app_module, "account_by_unique_id", return_value=source),
            patch.object(app_module, "account_by_ref", return_value=target),
            patch.object(app_module, "can_access_account", return_value=True),
        ):
            return self.client.post(
                f"/accounts/{ACCOUNT_ID}/merge-into",
                data={"csrf_token": "t", "target_account_ref": "acc-2"},
                follow_redirects=False,
            )

    def test_mismatched_nickname_redirects_back_to_the_panel(self):
        # A form submit that returns JSON navigates the browser onto the JSON
        # body, which is exactly the bug the operator hit. Any outcome here has
        # to be a redirect with a flash message.
        response = self._post_merge()
        self.assertEqual(303, response.status_code)
        self.assertNotIn("application/json", response.headers.get("content-type", ""))
        self.assertIn("/", response.headers.get("location", ""))


class WebUiQrProxyTests(unittest.TestCase):
    """The WebUI proxy must relay QR states instead of wrapping them as images."""

    def setUp(self):
        self.client = TestClient(app_module.app)
        self.principal = {
            "username": "admin",
            "role": "admin",
            "account_refs": [],
            "session_id": "session-1",
        }

    def _get_qr(self, upstream):
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "get_login_lock", return_value={"username": "admin", "session_id": ""}),
            patch.object(app_module, "owns_login_lock", return_value=True),
            patch.object(app_module.urllib.request, "urlopen", return_value=upstream),
        ):
            return self.client.get("/login-desktop/qr")

    def test_logged_in_json_body_is_relayed_not_wrapped_as_png(self):
        body = json.dumps(
            {
                "ok": False,
                "state": "qr_logged_in",
                "message": "检测到浏览器里还保留着登录状态，已重置，正在生成新的二维码",
                "logged_in": True,
                "retryable": True,
            }
        ).encode("utf-8")
        upstream = _FakeUpstream(body, status=202, retry_after=2)

        response = self._get_qr(upstream)

        self.assertEqual(202, response.status_code)
        self.assertIn("application/json", response.headers["content-type"])
        payload = response.json()
        self.assertEqual("qr_logged_in", payload["state"])
        self.assertTrue(payload["logged_in"])
        self.assertTrue(payload["retryable"])

    def test_busy_message_reaches_the_ui_in_chinese(self):
        body = json.dumps(
            {"ok": False, "state": "qr_page_busy", "message": "登录页正在处理上一个请求，请稍后重试"}
        ).encode("utf-8")
        upstream = _FakeUpstream(body, status=503, retry_after=2)

        response = self._get_qr(upstream)

        self.assertEqual(503, response.status_code)
        payload = response.json()
        self.assertIn("重试", payload["message"])
        self.assertTrue(payload["retryable"])
        self.assertNotIn("creator", payload["message"])

    def test_expired_qr_is_not_retryable(self):
        body = json.dumps(
            {
                "ok": False,
                "state": "qr_expired",
                "message": "二维码已过期，请点击“刷新二维码”",
                "retryable": False,
            }
        ).encode("utf-8")
        upstream = _FakeUpstream(body, status=409)

        response = self._get_qr(upstream)

        self.assertEqual(409, response.status_code)
        payload = response.json()
        self.assertFalse(payload["retryable"])
        self.assertIn("刷新二维码", payload["message"])

    def test_png_body_is_still_served_as_an_image(self):
        upstream = _FakeUpstream(b"fake-png", content_type="image/png")

        response = self._get_qr(upstream)

        self.assertEqual(200, response.status_code)
        self.assertEqual("image/png", response.headers["content-type"])
        self.assertEqual(b"fake-png", response.content)


class UnverifiedLoginAttributionTests(unittest.TestCase):
    """A page/transport failure must not be recorded as a logged-out account."""

    def setUp(self):
        self.temp_dir = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.users_path = Path(self.temp_dir.name) / "usersData.json"
        self.users_path.write_text("[]", encoding="utf-8")
        isolate_account_data(self, self.users_path)

    def _save(self, **kwargs):
        account, _action = app_module.save_exported_login_result(
            {
                "unique_id": ACCOUNT_ID,
                "username": "Tester",
                "cookies": [{"name": "sessionid", "value": "v", "domain": ".douyin.com", "path": "/"}],
            },
            is_healthy=False,
            **kwargs,
        )
        return account

    def test_structure_failure_is_not_recorded_as_login_required(self):
        account = self._save(
            verification_reason="creator identity card did not become ready within timeout",
            verification_category=friends_module.CATEGORY_STRUCTURE_CHANGED,
        )

        self.assertTrue(account["pending_login_verification"])
        self.assertNotIn("login_required", account)
        self.assertFalse(account["account_health"]["healthy"])
        self.assertEqual(
            friends_module.CATEGORY_STRUCTURE_CHANGED,
            account["account_health"]["category"],
        )
        preflight = streak_state.preflight_account(account)
        self.assertFalse(preflight["healthy"])
        self.assertEqual("login_verification_pending", preflight["category"])

    def test_network_failure_is_not_recorded_as_login_required(self):
        account = self._save(
            verification_reason="无法读取好友私信页，无法验证登录态：Timeout 60000ms exceeded",
            verification_category=friends_module.CATEGORY_NETWORK_UNAVAILABLE,
        )

        self.assertNotIn("login_required", account)
        self.assertEqual(
            friends_module.CATEGORY_NETWORK_UNAVAILABLE,
            account["account_health"]["category"],
        )
        # An unverified session still blocks sending until it is verified.
        self.assertFalse(streak_state.preflight_account(account)["healthy"])

    def test_login_failure_still_requires_login(self):
        account = self._save(
            verification_reason="账号登录已失效，请重新扫码登录",
            verification_category=friends_module.CATEGORY_LOGIN_REQUIRED,
        )

        self.assertTrue(account["login_required"])
        self.assertTrue(account["pending_login_verification"])
        self.assertEqual(
            friends_module.CATEGORY_LOGIN_REQUIRED,
            account["account_health"]["category"],
        )

    def test_missing_category_keeps_the_previous_login_failure_marking(self):
        account = self._save(verification_reason="登录态仍不可用")

        self.assertTrue(account["login_required"])
        self.assertEqual(
            friends_module.CATEGORY_LOGIN_REQUIRED,
            account["account_health"]["category"],
        )

    def test_page_failure_clears_a_previous_login_failure_marking(self):
        self._save(
            verification_reason="账号登录已失效，请重新扫码登录",
            verification_category=friends_module.CATEGORY_LOGIN_REQUIRED,
        )

        account = self._save(
            verification_reason="creator identity card did not become ready within timeout",
            verification_category=friends_module.CATEGORY_STRUCTURE_CHANGED,
        )

        self.assertNotIn("login_required", account)
        self.assertEqual(
            friends_module.CATEGORY_STRUCTURE_CHANGED,
            account["account_health"]["category"],
        )

    def test_login_failure_replaces_a_previous_page_failure(self):
        self._save(
            verification_reason="creator identity card did not become ready within timeout",
            verification_category=friends_module.CATEGORY_STRUCTURE_CHANGED,
        )

        account = self._save(
            verification_reason="账号登录已失效，请重新扫码登录",
            verification_category=friends_module.CATEGORY_LOGIN_REQUIRED,
        )

        self.assertTrue(account["login_required"])
        self.assertEqual(
            friends_module.CATEGORY_LOGIN_REQUIRED,
            account["account_health"]["category"],
        )


class IdentityReadBudgetTests(unittest.TestCase):
    """The identity read must get the full render budget and one retry."""

    def _run(self, failures):
        seen = []
        attempts = {"count": 0}

        async def fake_login_result(page_arg, context_arg, timeout_ms=300000):
            seen.append(timeout_ms)
            attempts["count"] += 1
            if attempts["count"] <= failures:
                raise RuntimeError("creator identity card did not become ready within timeout")
            return {"unique_id": " 8940433898798 ", "username": " srx666 "}

        context = _FakeContext(_FakePage(sub_app_count=1))
        browser = _FakeBrowser(context)

        async def fake_get_browser(*args, **kwargs):
            return _FakePlaywright(), browser

        with (
            patch.object(friends_module, "get_browser", side_effect=fake_get_browser),
            patch.object(friends_module, "collect_login_result", side_effect=fake_login_result),
        ):
            result = asyncio.run(
                friends_module._fetch_account_friends_once(
                    {"cookies": [{"name": "sessionid", "value": "v"}]},
                    "direct",
                    auth_only=True,
                )
            )
        return result, seen, context._page

    def test_first_attempt_uses_the_full_render_budget(self):
        result, seen, _page = self._run(failures=0)

        self.assertEqual("8940433898798", result["unique_id"])
        self.assertEqual([friends_module.LOGIN_IDENTITY_TIMEOUT_MS], seen)

    def test_slow_identity_read_is_retried_once_with_a_shorter_budget(self):
        result, seen, page = self._run(failures=1)

        self.assertEqual("8940433898798", result["unique_id"])
        self.assertEqual(2, len(seen))
        self.assertEqual(friends_module.LOGIN_IDENTITY_TIMEOUT_MS, seen[0])
        self.assertEqual(
            min(
                friends_module.LOGIN_IDENTITY_TIMEOUT_MS,
                friends_module.IDENTITY_RETRY_BUDGET_MS,
            ),
            seen[1],
        )
        self.assertLess(seen[1], seen[0])
        # The reload before the retry is capped, not a full navigation timeout.
        self.assertEqual(
            [friends_module.IDENTITY_RELOAD_TIMEOUT_SECONDS * 1000],
            page.reload_timeouts,
        )

    def test_visible_login_form_fails_fast_as_login_required(self):
        seen = []

        async def fake_login_result(page_arg, context_arg, timeout_ms=300000):
            seen.append(timeout_ms)
            raise RuntimeError(login_module.LOGIN_REQUIRED_MESSAGE)

        context = _FakeContext(_FakePage(sub_app_count=1))
        browser = _FakeBrowser(context)

        async def fake_get_browser(*args, **kwargs):
            return _FakePlaywright(), browser

        with (
            patch.object(friends_module, "get_browser", side_effect=fake_get_browser),
            patch.object(friends_module, "collect_login_result", side_effect=fake_login_result),
            self.assertRaises(friends_module.FriendRefreshError) as caught,
        ):
            asyncio.run(
                friends_module._fetch_account_friends_once(
                    {"cookies": [{"name": "sessionid", "value": "v"}]},
                    "direct",
                    auth_only=True,
                )
            )

        self.assertEqual(
            friends_module.CATEGORY_LOGIN_REQUIRED,
            caught.exception.category,
        )
        self.assertEqual(1, len(seen))

    def test_full_budget_timeout_is_not_retried(self):
        seen = []

        async def slow_failure(page_arg, context_arg, timeout_ms=300000):
            seen.append(timeout_ms)
            await asyncio.sleep(0.05)
            raise RuntimeError("creator identity card did not become ready within timeout")

        context = _FakeContext(_FakePage(sub_app_count=1))
        browser = _FakeBrowser(context)

        async def fake_get_browser(*args, **kwargs):
            return _FakePlaywright(), browser

        with (
            patch.object(friends_module, "get_browser", side_effect=fake_get_browser),
            patch.object(friends_module, "collect_login_result", side_effect=slow_failure),
            patch.object(friends_module, "IDENTITY_RETRY_FAST_FAIL_MS", 1),
            self.assertRaises(RuntimeError) as caught,
        ):
            asyncio.run(
                friends_module._fetch_account_friends_once(
                    {"cookies": [{"name": "sessionid", "value": "v"}]},
                    "direct",
                    auth_only=True,
                )
            )

        self.assertEqual(1, len(seen))
        self.assertEqual(
            friends_module.CATEGORY_STRUCTURE_CHANGED,
            friends_module.classify_refresh_error(caught.exception),
        )


class _IdentityLocator:
    def __init__(self, texts):
        self._texts = list(texts)

    @property
    def first(self):
        return self

    async def count(self):
        return len(self._texts)

    async def is_visible(self):
        return bool(self._texts)

    async def inner_text(self):
        return self._texts[0] if self._texts else ""


class _IdentityPage:
    """Page double keyed by selector, for the login identity reader."""

    def __init__(self, visible=None, url="https://creator.douyin.com/"):
        self.url = url
        self.visible = dict(visible or {})

    def locator(self, selector):
        return _IdentityLocator(self.visible.get(selector, []))


class LoginIdentityReaderTests(unittest.TestCase):
    CARD_TEXT = "srx666抖音号：8940433898798凡我所失，皆非我所有关注4428粉丝"

    def test_nickname_falls_back_to_the_card_text_when_the_name_node_is_gone(self):
        page = _IdentityPage(
            {
                login_module.READY_SELECTOR: [self.CARD_TEXT],
                login_module.XPATHS["unique_id"]: ["抖音号：8940433898798"],
            }
        )

        unique_id, username = asyncio.run(
            login_module.wait_for_logged_in_identity(page, timeout_ms=1000)
        )

        self.assertEqual("8940433898798", unique_id)
        self.assertEqual("srx666", username)

    def test_preferred_name_node_still_wins(self):
        page = _IdentityPage(
            {
                login_module.READY_SELECTOR: [self.CARD_TEXT],
                login_module.XPATHS["unique_id"]: ["抖音号：8940433898798"],
                login_module.XPATHS["name"]: ["srx666"],
            }
        )

        unique_id, username = asyncio.run(
            login_module.wait_for_logged_in_identity(page, timeout_ms=1000)
        )

        self.assertEqual("8940433898798", unique_id)
        self.assertEqual("srx666", username)

    def test_hidden_name_node_falls_back_to_the_card_text(self):
        page = _IdentityPage(
            {
                login_module.READY_SELECTOR: [self.CARD_TEXT],
                login_module.XPATHS["unique_id"]: ["抖音号：8940433898798"],
                login_module.XPATHS["name"]: [],
                login_module.NICKNAME_FALLBACK_SELECTORS[0]: [],
            }
        )

        _unique_id, username = asyncio.run(
            login_module.wait_for_logged_in_identity(page, timeout_ms=1000)
        )

        self.assertEqual("srx666", username)

    def test_visible_login_form_is_reported_as_login_required(self):
        page = _IdentityPage(
            {
                login_module.LOGIN_FORM_SELECTORS[0]: ["扫码登录"],
            }
        )

        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(login_module.wait_for_logged_in_identity(page, timeout_ms=1000))

        self.assertIn("登录", str(caught.exception))
        self.assertEqual(
            friends_module.CATEGORY_LOGIN_REQUIRED,
            friends_module.classify_refresh_error(caught.exception),
        )

    def test_card_without_readable_identity_is_a_structure_problem(self):
        page = _IdentityPage(
            {
                login_module.READY_SELECTOR: [self.CARD_TEXT],
            }
        )

        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(login_module.wait_for_logged_in_identity(page, timeout_ms=1000))

        self.assertEqual(
            friends_module.CATEGORY_STRUCTURE_CHANGED,
            friends_module.classify_refresh_error(caught.exception),
        )

    def test_card_that_never_renders_is_a_structure_problem(self):
        page = _IdentityPage()

        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(login_module.wait_for_logged_in_identity(page, timeout_ms=100))

        self.assertEqual(
            friends_module.CATEGORY_STRUCTURE_CHANGED,
            friends_module.classify_refresh_error(caught.exception),
        )

    def test_card_text_without_the_douyin_id_marker_is_not_guessed(self):
        self.assertEqual(("", ""), login_module.identity_from_card_text("srx666 关注 4428"))
        self.assertEqual(
            ("srx666", "srx666123456"),
            login_module.identity_from_card_text("srx666抖音号：srx666123456凡我所失"),
        )


if __name__ == "__main__":
    unittest.main()
