"""Offline sender route-reuse contract; all browser handles are synthetic."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import Error as PlaywrightError

from core import tasks


PROFILE = {
    "enabled": True,
    "root": "offline-profile",
    "seedCookiesWhenEmpty": False,
    "syncStoredCookiesBeforeRun": False,
    "refreshStoredCookiesAfterLogin": False,
}
STRATEGY = {
    "shuffleTargets": False,
    "accountStartDelaySecondsMin": 0,
    "accountStartDelaySecondsMax": 0,
    "messageIntervalSecondsMin": 0,
    "messageIntervalSecondsMax": 0,
}
USER = {"username": "offline", "unique_id": "offline-route", "cookies": [], "targets": ["friend"]}


async def _no_sleep(*args, **kwargs):
    return None


async def _empty_scan(*args, **kwargs):
    if False:
        yield None


def _context(page, *, close=None):
    return SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        close=AsyncMock(side_effect=close),
        set_default_navigation_timeout=Mock(),
        set_default_timeout=Mock(),
    )


def _page(*, goto, url="about:blank", committed=False):
    handlers = {}
    def on(event, callback):
        handlers.setdefault(event, []).append(callback)
    def remove_listener(event, callback):
        handlers[event].remove(callback)
    async def fail_after_commit(*args, **kwargs):
        for callback in handlers.get("framenavigated", []):
            callback(SimpleNamespace(url="http://synthetic.invalid/"))
            callback(SimpleNamespace(url="about:blank"))
        raise goto
    return SimpleNamespace(url=url, goto=AsyncMock(side_effect=fail_after_commit if committed else goto),
                           close=AsyncMock(), on=Mock(side_effect=on),
                           remove_listener=Mock(side_effect=remove_listener))


def _common_sender_patches():
    collector = SimpleNamespace(
        attach=lambda page: collector,
        detach=lambda: None,
        pending_tasks=lambda: (),
        aclose=AsyncMock(return_value=True),
    )
    return {
        "_acquire_browser_account_lock": AsyncMock(return_value=(Mock(), Mock(), "token")),
        "_release_browser_account_lock": Mock(),
        "_sleep_with_log": AsyncMock(),
        "get_userData": lambda **kwargs: [dict(USER)],
        "_find_matching_account": lambda accounts, user: user,
        "_page_has_login_required_prompt": AsyncMock(return_value=(False, "")),
        "ensure_not_login_required": AsyncMock(),
        "_scan_delivery_targets": _empty_scan,
        "_persist_browser_send_failure": Mock(),
        "_persist_account_send_failure": Mock(),
        "friend_identity": SimpleNamespace(UserDetailCollector=lambda *args, **kwargs: collector),
    }


async def _run_persistent():
    await tasks.do_user_task(
        None, dict(USER), asyncio.Semaphore(1), STRATEGY, PROFILE,
        {"maxScanSeconds": 1}, "direct", run_id="offline-route",
    )


class SenderRouteReuseTests(unittest.TestCase):
    def setUp(self):
        tasks._BLOCKED_BROWSER_LOCKS.clear()

    def tearDown(self):
        tasks._BLOCKED_BROWSER_LOCKS.clear()

    def test_no_work_does_not_preflight_or_launch_browser(self):
        async def exercise():
            with patch.object(tasks, "select_douyin_network_mode", AsyncMock()) as preflight, \
                 patch.object(tasks, "get_browser", AsyncMock()) as launch:
                await tasks._run_browser_tasks(
                    {"taskCount": 1, "multiTask": False, "persistentBrowserProfiles": {"enabled": True}},
                    [],
                )
            preflight.assert_not_awaited()
            launch.assert_not_awaited()
        asyncio.run(exercise())

    def test_persistent_sender_uses_configured_first_route_without_anonymous_preflight_or_launch(self):
        async def exercise():
            config = {"taskCount": 1, "multiTask": False, "persistentBrowserProfiles": {"enabled": True}}
            with patch.object(tasks, "select_douyin_network_mode", AsyncMock()) as preflight, \
                 patch.object(tasks, "get_browser", AsyncMock()) as anonymous_launch, \
                 patch.object(tasks, "douyin_network_modes", return_value=("direct", "mihomo"), create=True), \
                 patch.object(tasks, "do_user_task", AsyncMock()) as run_user:
                await tasks._run_browser_tasks(config, [dict(USER)])
            preflight.assert_not_awaited()
            anonymous_launch.assert_not_awaited()
            self.assertEqual("direct", run_user.await_args.args[6])
        asyncio.run(exercise())

    def test_direct_precommit_network_failure_closes_profile_before_one_mihomo_reopen(self):
        self._assert_safe_fallback("net::ERR_CONNECTION_REFUSED")

    def test_precommit_connection_timeout_can_use_the_fallback_route(self):
        self._assert_safe_fallback("net::ERR_CONNECTION_TIMED_OUT")

    def _assert_safe_fallback(self, net_error):
        async def exercise():
            events = []
            first = _page(goto=PlaywrightError(net_error))
            second = _page(goto=[SimpleNamespace(status=200), SimpleNamespace(status=200)])
            first_context = _context(first, close=lambda: events.append("direct-closed"))
            second_context = _context(second)
            driver_one = SimpleNamespace(stop=AsyncMock())
            driver_two = SimpleNamespace(stop=AsyncMock())
            launches = []

            async def launch(name, **kwargs):
                launches.append(kwargs["network_mode"])
                if len(launches) == 2:
                    self.assertEqual(["direct-closed"], events)
                    return driver_two, second_context, "mihomo-profile"
                return driver_one, first_context, "direct-profile"

            common = _common_sender_patches()
            dispatch = AsyncMock()
            common["_dispatch_browser_enter_if_current"] = dispatch
            with patch.object(tasks, "douyin_network_modes", return_value=("direct", "mihomo"), create=True), \
                 patch.object(tasks, "get_persistent_browser_context", AsyncMock(side_effect=launch)), \
                 patch.object(tasks, "prepare_profile_cookies", AsyncMock(return_value=SimpleNamespace(account=dict(USER)))), \
                 patch.object(tasks.asyncio, "sleep", _no_sleep), \
                 patch.multiple(tasks, **common):
                await _run_persistent()

            self.assertEqual(["direct", "mihomo"], launches)
            dispatch.assert_not_awaited()
            first_context.close.assert_awaited_once()
            second_context.close.assert_awaited_once()
            first.goto.assert_awaited()
        asyncio.run(exercise())

    def test_http_risk_8610_and_committed_timeout_never_reopen_on_next_route(self):
        cases = [
            ("http", PlaywrightError("HTTP 503"), "about:blank"),
            ("risk", PlaywrightError("net::ERR_CONNECTION_REFUSED login_required"), "about:blank"),
            ("8610", PlaywrightError("net::ERR_CONNECTION_REFUSED server rejected 8610"), "about:blank"),
            ("committed-timeout", PlaywrightError("net::ERR_CONNECTION_TIMED_OUT"), tasks.CREATOR_HOME_URL),
            ("committed-returned-blank", PlaywrightError("net::ERR_NAME_NOT_RESOLVED"), "about:blank"),
        ]
        for label, error, url in cases:
            with self.subTest(label=label):
                async def exercise():
                    page = _page(goto=error, url=url, committed=label == "committed-returned-blank")
                    context = _context(page)
                    launches = []

                    async def launch(name, **kwargs):
                        launches.append(kwargs["network_mode"])
                        return SimpleNamespace(stop=AsyncMock()), context, "direct-profile"

                    common = _common_sender_patches()
                    with patch.object(tasks, "douyin_network_modes", return_value=("direct", "mihomo"), create=True), \
                         patch.object(tasks, "get_persistent_browser_context", AsyncMock(side_effect=launch)), \
                         patch.object(tasks, "prepare_profile_cookies", AsyncMock(return_value=SimpleNamespace(account=dict(USER)))), \
                         patch.object(tasks.asyncio, "sleep", _no_sleep), \
                         patch.multiple(tasks, **common):
                        await _run_persistent()

                    self.assertEqual(["direct"], launches)
                    context.close.assert_awaited_once()
                asyncio.run(exercise())

    def test_cleanup_not_proven_blocks_route_reopen(self):
        async def exercise():
            page = _page(goto=PlaywrightError("net::ERR_CONNECTION_REFUSED"))
            context = _context(page, close=RuntimeError("close failed"))
            launches = []

            async def launch(name, **kwargs):
                launches.append(kwargs["network_mode"])
                return SimpleNamespace(stop=AsyncMock()), context, "direct-profile"

            common = _common_sender_patches()
            with patch.object(tasks, "douyin_network_modes", return_value=("direct", "mihomo"), create=True), \
                 patch.object(tasks, "get_persistent_browser_context", AsyncMock(side_effect=launch)), \
                 patch.object(tasks, "prepare_profile_cookies", AsyncMock(return_value=SimpleNamespace(account=dict(USER)))), \
                 patch.object(tasks.asyncio, "sleep", _no_sleep), \
                 patch.multiple(tasks, **common):
                await _run_persistent()

            self.assertEqual(["direct"], launches)
            context.close.assert_awaited_once()
        asyncio.run(exercise())

    def test_route_fallback_shares_one_whole_account_deadline(self):
        async def exercise():
            completed = []
            attempts = []
            async def attempt(*args, **kwargs):
                attempts.append(args[6])
                await asyncio.sleep(.6)
                if len(attempts) == 1:
                    raise tasks._PreDispatchNetworkRouteFallback()
                completed.append(True)
            common = _common_sender_patches()
            record_failure = Mock()
            with patch.multiple(tasks, **common), \
                 patch.object(tasks, 'douyin_network_modes', return_value=('direct', 'mihomo')), \
                 patch.object(tasks, '_browser_account_timeout_seconds', return_value=1.0), \
                 patch.object(tasks, '_do_user_task_locked', side_effect=attempt), \
                 patch.object(tasks, '_record_browser_stage_failure', record_failure):
                await _run_persistent()
            self.assertEqual(['direct', 'mihomo'], attempts)
            self.assertEqual([], completed)
            record_failure.assert_called_once()
        asyncio.run(exercise())

    def test_nonpersistent_path_keeps_existing_preflight_compatibility(self):
        async def exercise():
            browser = SimpleNamespace(close=AsyncMock())
            playwright = SimpleNamespace(stop=AsyncMock())
            config = {"taskCount": 1, "multiTask": False, "persistentBrowserProfiles": {"enabled": False}}
            with patch.object(tasks, "select_douyin_network_mode", AsyncMock(return_value="direct")) as preflight, \
                 patch.object(tasks, "get_browser", AsyncMock(return_value=(playwright, browser))) as launch, \
                 patch.object(tasks, "do_user_task", AsyncMock()) as run_user:
                await tasks._run_browser_tasks(config, [dict(USER)])
            preflight.assert_awaited_once_with(
                tasks.CREATOR_HOME_URL, profile_name="uid-offline-route"
            )
            launch.assert_not_awaited()
            self.assertEqual("direct", run_user.await_args.args[6])
            browser.close.assert_not_awaited()
            playwright.stop.assert_not_awaited()
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
