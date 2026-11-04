"""Refresh entry points share the generation-aware profile-cookie policy."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core import friends, groups, patrol, profile_cookies
from core import identity as account_identity
from core import tasks


COOKIE = {"name": "sessionid", "value": "saved", "domain": ".douyin.com", "path": "/"}
PROFILE = {
    "enabled": True,
    "root": "synthetic-profile",
    "syncStoredCookiesBeforeRun": True,
    "seedCookiesWhenEmpty": True,
    "refreshStoredCookiesAfterLogin": True,
}
ACCOUNT = {
    "account_ref": "cookie-entry-account",
    "unique_id": "1234567890",
    "username": "cookie-entry",
    "login_generation": "generation-a",
    "cookies": [COOKIE],
    account_identity.FIELD_UID: "1234567890",
    account_identity.FIELD_BINDING: {
        "source": account_identity.IDENTITY_SOURCE,
        "version": 1,
    },
}


class _Page:
    def __init__(self):
        self.closed = False

    async def goto(self, *args, **kwargs):
        return None

    async def close(self):
        self.closed = True

    def on(self, *args, **kwargs):
        return None

    def remove_listener(self, *args, **kwargs):
        return None


class _Context:
    def __init__(self, page=None):
        self.page = page or _Page()
        self.add_cookies = AsyncMock()
        self.close = AsyncMock()
        self.route = AsyncMock()
        self.cookies = AsyncMock(return_value=[COOKIE])

    def set_default_navigation_timeout(self, value):
        del value

    def set_default_timeout(self, value):
        del value

    async def new_page(self):
        return self.page


class _Driver:
    def __init__(self):
        self.stop = AsyncMock()


class _Collector:
    def attach(self, page):
        del page
        return self

    async def drain(self, timeout=None):
        del timeout

    def entries(self):
        return []

    def summary(self):
        return {}

    def pending_tasks(self):
        return ()

    async def aclose(self):
        return True


async def _run_stage(stage, operation, **kwargs):
    del stage, kwargs
    return await operation


async def _ignore_cancellation_until(gate):
    while not gate.is_set():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            continue


async def _drain_uncooperative_operations(gate):
    gate.set()
    pending = [task for task in tasks._PENDING_BROWSER_TASKS if not task.done()]
    if pending:
        await asyncio.wait(pending, timeout=.2)


def _fast_stage_runner():
    original = tasks._run_browser_stage

    async def run(stage, operation, **kwargs):
        del kwargs
        return await original(stage, operation, timeout_seconds=.01, cancel_grace_seconds=.01)

    return run


class RefreshCookieEntrypointTests(unittest.TestCase):
    def test_friend_prepare_timeout_or_close_failure_retains_lock(self):
        async def run(stage):
            gate = asyncio.Event()
            context = _Context()
            driver = _Driver()
            session = SimpleNamespace(account=dict(ACCOUNT))

            async def stuck(*args):
                del args
                await _ignore_cancellation_until(gate)

            prepare = AsyncMock(return_value=session)
            publish = AsyncMock(return_value=True)
            if stage == "prepare":
                prepare.side_effect = stuck
            else:
                context.close.side_effect = RuntimeError("synthetic context close failure")
            release = Mock()
            retain = Mock()
            try:
                with (
                    patch.object(friends, "normalize_persistent_profile_config", return_value=PROFILE),
                    patch.object(friends, "get_persistent_browser_context", new=AsyncMock(return_value=(driver, context, "synthetic-profile"))),
                    patch.object(friends, "acquire_browser_account_lock", new=AsyncMock(return_value=("lock", "path", "token"))),
                    patch.object(friends, "release_browser_account_lock", release),
                    patch.object(friends, "retain_browser_account_lock_for_cleanup", retain),
                    patch.object(friends, "collect_friend_names", new=AsyncMock(return_value=friends.FriendScanResult(["friend"], complete=True))),
                    patch.object(friends, "UserDetailCollector", return_value=_Collector()),
                    patch.object(friends.asyncio, "sleep", new=AsyncMock()),
                    patch.object(profile_cookies, "prepare_profile_cookies", prepare),
                    patch.object(profile_cookies, "publish_profile_cookies", publish),
                    patch.object(tasks, "_run_browser_stage", new=_fast_stage_runner()),
                ):
                    if stage == "prepare":
                        with self.assertRaises(tasks.BrowserStageTimeout):
                            await friends._fetch_account_friends_once(dict(ACCOUNT), "direct")
                    else:
                        await friends._fetch_account_friends_once(dict(ACCOUNT), "direct")
                release.assert_not_called()
                retain.assert_called_once()
                publish.assert_not_awaited()
            finally:
                await _drain_uncooperative_operations(gate)

        asyncio.run(run("prepare"))
        asyncio.run(run("close"))

    def test_patrol_prepare_failure_preserves_failed_startup_close_barrier(self):
        async def run():
            user = dict(ACCOUNT); context = _Context(); driver = _Driver()
            context.close.side_effect = RuntimeError('synthetic context close failure')
            release = Mock(); retain = Mock()
            with patch.object(patrol, 'get_persistent_browser_context', new=AsyncMock(return_value=(driver, context, 'synthetic-profile'))), \
                 patch.object(profile_cookies, 'prepare_profile_cookies', new=AsyncMock(side_effect=RuntimeError('synthetic prepare failure'))), \
                 patch.object(patrol, 'acquire_browser_account_lock', new=AsyncMock(return_value=('lock', 'path', 'token'))), \
                 patch.object(patrol, 'release_browser_account_lock', release), \
                 patch.object(patrol, 'retain_browser_account_lock_for_cleanup', retain), \
                 patch.object(patrol, '_persist_patrol'):
                await patrol.patrol_account(user, profile_config=PROFILE, lock_wait=0)
            release.assert_not_called()
            retain.assert_called_once()
            driver.stop.assert_awaited_once()
        asyncio.run(run())

    def test_patrol_cookie_stage_startup_timeout_sets_barrier_and_retains_lock(self):
        async def run():
            gate = asyncio.Event()
            user = dict(ACCOUNT)
            context = _Context()
            driver = _Driver()
            release = Mock()
            retain = Mock()

            async def prepare(*args):
                del args
                await _ignore_cancellation_until(gate)

            try:
                with (
                    patch.object(patrol, "get_persistent_browser_context", new=AsyncMock(return_value=(driver, context, "synthetic-profile"))),
                    patch.object(patrol, "acquire_browser_account_lock", new=AsyncMock(return_value=("lock", "path", "token"))),
                    patch.object(patrol, "release_browser_account_lock", release),
                    patch.object(patrol, "retain_browser_account_lock_for_cleanup", retain),
                    patch.object(patrol, "_persist_patrol"),
                    patch.object(profile_cookies, "prepare_profile_cookies", prepare),
                    patch.object(tasks, "_run_browser_stage", new=_fast_stage_runner()),
                ):
                    result = await patrol.patrol_account(
                        user, profile_config=PROFILE, lock_wait=0
                    )
                self.assertEqual("browser_stage_timeout", result["login"]["category"])
                # patrol_account consumes the private barrier when it retains
                # the lock; the durable blocked lock is the sticky outcome.
                self.assertFalse(user.get("_patrol_uid_cleanup_blocked"))
                release.assert_not_called()
                retain.assert_called_once()
            finally:
                await _drain_uncooperative_operations(gate)

        asyncio.run(run())

    def test_group_prepare_timeout_and_context_close_failure_retain_lock(self):
        async def run(kind):
            gate = asyncio.Event()
            page = _Page()
            rows = Mock(all=AsyncMock(return_value=[Mock(is_visible=AsyncMock(return_value=True))]))
            page.locator = Mock(return_value=rows)
            page.get_by_role = Mock(return_value=Mock(click=AsyncMock()))
            context = _Context(page)
            driver = _Driver()
            session = SimpleNamespace(account=dict(ACCOUNT))
            release = Mock()
            retain = Mock()

            async def publish(*args):
                del args
                await _ignore_cancellation_until(gate)

            if kind == "close":
                context.close.side_effect = RuntimeError("offline close failure")
            exporter = AsyncMock()
            try:
                with (
                    patch.object(groups, "normalize_persistent_profile_config", return_value=PROFILE),
                    patch.object(groups, "douyin_network_modes", return_value=["direct"]),
                    patch.object(groups, "get_persistent_browser_context", new=AsyncMock(return_value=(driver, context, "synthetic-profile"))),
                    patch.object(groups, "acquire_browser_account_lock", new=AsyncMock(return_value=("lock", "path", "token"))),
                    patch.object(groups, "release_browser_account_lock", release),
                    patch.object(groups, "retain_browser_account_lock_for_cleanup", retain),
                    patch.object(groups, "_wait_for_chat_or_login", new=AsyncMock()),
                    patch.object(groups, "_activate_group_tab", new=AsyncMock(return_value="")),
                    patch.object(groups, "collect_group_rows", new=AsyncMock(return_value=[{"name": "group", "conversationId": "1"}])),
                    patch.object(profile_cookies, "prepare_profile_cookies", publish if kind == "prepare" else AsyncMock(return_value=session)),
                    patch.object(profile_cookies, "publish_profile_cookies", exporter),
                    patch.object(tasks, "_run_browser_stage", new=_fast_stage_runner()),
                ):
                    if kind == "prepare":
                        with self.assertRaises(tasks.BrowserStageTimeout):
                            await groups.fetch_account_groups(dict(ACCOUNT))
                    else:
                        await groups.fetch_account_groups(dict(ACCOUNT))
                exporter.assert_not_awaited()
                release.assert_not_called()
                retain.assert_called_once()
            finally:
                await _drain_uncooperative_operations(gate)

        asyncio.run(run("prepare"))
        asyncio.run(run("close"))

    def test_friend_refresh_preserves_auth_and_never_exports_unverified_profile(self):
        async def run(complete):
            context = _Context()
            driver = _Driver()
            session = SimpleNamespace(account=dict(ACCOUNT))
            prepare = AsyncMock(return_value=session)
            publish = AsyncMock(return_value=True)
            lock = AsyncMock(return_value=("lock", "path", "token"))
            with (
                patch.object(friends, "normalize_persistent_profile_config", return_value=PROFILE),
                patch.object(friends, "get_persistent_browser_context", new=AsyncMock(return_value=(driver, context, "synthetic-profile"))),
                patch.object(friends, "acquire_browser_account_lock", new=lock),
                patch.object(friends, "release_browser_account_lock"),
                patch.object(friends, "collect_friend_names", new=AsyncMock(return_value=friends.FriendScanResult(["friend"], complete=complete))),
                patch.object(friends, "UserDetailCollector", return_value=_Collector()),
                patch.object(friends.asyncio, "sleep", new=AsyncMock()),
                patch.object(profile_cookies, "prepare_profile_cookies", prepare),
                patch.object(profile_cookies, "publish_profile_cookies", publish),
                patch("core.tasks._run_browser_stage", new=AsyncMock(side_effect=_run_stage)),
            ):
                result = await friends._fetch_account_friends_once(dict(ACCOUNT), "direct")
            return result, context, prepare, publish

        complete, context, prepare, publish = asyncio.run(run(True))
        self.assertTrue(complete.complete)
        prepare.assert_awaited_once()
        publish.assert_not_awaited()
        context.add_cookies.assert_not_awaited()

        incomplete, _context, prepare, publish = asyncio.run(run(False))
        self.assertFalse(incomplete.complete)
        prepare.assert_awaited_once()
        publish.assert_not_awaited()

    def test_patrol_prepares_at_startup_and_exports_only_after_verified_complete_scan(self):
        context = _Context()
        driver = _Driver()
        session = SimpleNamespace(account=dict(ACCOUNT))
        prepare = AsyncMock(return_value=session)
        publish = AsyncMock(return_value=True)
        watcher = Mock(
            attach=Mock(return_value=None),
            drain=AsyncMock(),
            detach=Mock(),
            pending_tasks=Mock(return_value=()),
            aclose=AsyncMock(return_value=True),
            uid=ACCOUNT["unique_id"],
        )
        watcher.attach.return_value = watcher
        with (
            patch.object(patrol, "get_persistent_browser_context", new=AsyncMock(return_value=(driver, context, "synthetic-profile"))),
            patch.object(patrol, "collect_friend_names", new=AsyncMock(return_value=friends.FriendScanResult(["friend"], complete=True))),
            patch.object(patrol.friend_identity, "UserDetailCollector", return_value=_Collector()),
            patch.object(patrol, "LoginUidWatcher", return_value=watcher),
            patch.object(patrol, "_detect_login_prompt", new=AsyncMock(return_value=(False, ""))),
            patch.object(patrol, "_collect_patrol_groups", new=AsyncMock(return_value=(None, ""))),
            patch.object(patrol.asyncio, "sleep", new=AsyncMock()),
            patch.object(profile_cookies, "prepare_profile_cookies", prepare),
            patch.object(profile_cookies, "publish_profile_cookies", publish),
            patch("core.tasks._run_browser_stage", new=AsyncMock(side_effect=_run_stage)),
        ):
            inspection = asyncio.run(patrol._inspect_once(dict(ACCOUNT), "direct", PROFILE))
        self.assertEqual(["friend"], list(inspection["friends"]))
        prepare.assert_awaited_once()
        publish.assert_awaited_once()
        context.add_cookies.assert_not_awaited()

    def test_group_prepare_cancellation_still_closes_without_export(self):
        page = _Page()
        rows = Mock(all=AsyncMock(return_value=[Mock(is_visible=AsyncMock(return_value=True))]))
        page.locator = Mock(return_value=rows)
        page.get_by_role = Mock(return_value=Mock(click=AsyncMock()))
        context = _Context(page)
        driver = _Driver()
        session = SimpleNamespace(account=dict(ACCOUNT))
        prepare = AsyncMock(return_value=session)
        prepare.side_effect = asyncio.CancelledError()
        publish = AsyncMock()
        release = Mock()
        with (
            patch.object(groups, "normalize_persistent_profile_config", return_value=PROFILE),
            patch.object(groups, "douyin_network_modes", return_value=["direct"]),
            patch.object(groups, "get_persistent_browser_context", new=AsyncMock(return_value=(driver, context, "synthetic-profile"))),
            patch.object(groups, "acquire_browser_account_lock", new=AsyncMock(return_value=("lock", "path", "token"))),
            patch.object(groups, "release_browser_account_lock", release),
            patch.object(groups, "_wait_for_chat_or_login", new=AsyncMock()),
            patch.object(groups, "_activate_group_tab", new=AsyncMock(return_value="")),
            patch.object(groups, "collect_group_rows", new=AsyncMock(return_value=[{"name": "group", "conversationId": "1"}])),
            patch.object(profile_cookies, "prepare_profile_cookies", prepare),
            patch.object(profile_cookies, "publish_profile_cookies", publish),
            patch("core.tasks._run_browser_stage", new=AsyncMock(side_effect=_run_stage)),
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(groups.fetch_account_groups(dict(ACCOUNT)))
        prepare.assert_awaited_once()
        publish.assert_not_awaited()
        context.add_cookies.assert_not_awaited()
        context.close.assert_awaited_once()
        driver.stop.assert_awaited_once()
        release.assert_called_once_with("lock", "path", "token", ACCOUNT["username"])


if __name__ == "__main__":
    unittest.main()
