"""Offline lifecycle coverage for refresh and patrol UID readers."""
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch

import unittest

from core import friends, patrol, tasks


CASE = unittest.TestCase()


class Resource:
    def __init__(self):
        self.close = AsyncMock()
        self.stop = AsyncMock()


class Context(Resource):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.add_cookies = AsyncMock()
        self.new_page = AsyncMock(return_value=page)
        self.set_default_navigation_timeout = Mock()
        self.set_default_timeout = Mock()


class WaitingCollector:
    def __init__(self, *, complete=True):
        self.complete = complete
        self.attached = False
        self.aclose_calls = 0
        self._pending = set()

    def attach(self, page):
        self.attached = True
        return self

    async def drain(self, timeout=None):
        return None

    def entries(self):
        return []

    def summary(self):
        return {}

    def pending_tasks(self):
        return tuple(task for task in self._pending if not task.done())

    async def aclose(self):
        self.aclose_calls += 1
        if self.complete:
            return True
        for task in self.pending_tasks():
            task.cancel()
        return False


def account():
    return {"username": "synthetic", "unique_id": "uid-synthetic", "cookies": [{"name": "sid"}], "targets": ["friend"]}


def test_friend_refresh_cancellation_closes_resources_and_releases_lock():
    async def run():
        user = account()
        page = Resource()
        page.goto = AsyncMock()
        context = Context(page)
        browser = Resource()
        browser.new_context = AsyncMock(return_value=context)
        playwright = Resource()
        collector = WaitingCollector()
        scan_started = asyncio.Event()
        release = Mock()

        async def scan(_page, **_kwargs):
            scan_started.set()
            await asyncio.Event().wait()

        with ExitStack() as stack:
            stack.enter_context(patch.dict(tasks._BLOCKED_BROWSER_LOCKS, {}, clear=True))
            stack.enter_context(patch.object(friends, "get_config", return_value={}))
            stack.enter_context(patch.object(friends, "get_browser", AsyncMock(return_value=(playwright, browser))))
            stack.enter_context(patch.object(friends, "collect_friend_names", scan))
            stack.enter_context(patch.object(friends.asyncio, "sleep", AsyncMock()))
            stack.enter_context(patch.object(friends, "UserDetailCollector", return_value=collector))
            stack.enter_context(patch.object(friends, "acquire_browser_account_lock", AsyncMock(return_value=(Mock(), "lock", "token"))))
            stack.enter_context(patch.object(friends, "release_browser_account_lock", release))
            task = asyncio.create_task(friends._fetch_account_friends_once(user, "direct"))
            await asyncio.wait_for(scan_started.wait(), .2)
            task.cancel()
            with CASE.assertRaises(asyncio.CancelledError):
                await task

        assert collector.aclose_calls == 1
        assert release.call_count == 1
        page.close.assert_awaited_once()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    asyncio.run(run())


def test_friend_refresh_stubborn_reader_blocks_account_and_stops_route_retry():
    async def run():
        user = account()
        page = Resource()
        page.goto = AsyncMock()
        context = Context(page)
        browser = Resource()
        browser.new_context = AsyncMock(return_value=context)
        playwright = Resource()
        gate = asyncio.Event()
        collector = WaitingCollector(complete=False)

        async def stubborn_reader():
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await gate.wait()

        reader = asyncio.create_task(stubborn_reader())
        collector._pending.add(reader)
        release = Mock()
        start_browser = AsyncMock(return_value=(playwright, browser))

        try:
            with ExitStack() as stack:
                stack.enter_context(patch.dict(tasks._BLOCKED_BROWSER_LOCKS, {}, clear=True))
                stack.enter_context(patch.object(friends, "get_config", return_value={}))
                stack.enter_context(patch.object(friends, "douyin_network_modes", return_value=["direct", "proxy"]))
                stack.enter_context(patch.object(friends, "get_browser", start_browser))
                stack.enter_context(patch.object(friends, "collect_friend_names", AsyncMock(return_value=friends.FriendScanResult(["friend"], complete=True))))
                stack.enter_context(patch.object(friends, "UserDetailCollector", return_value=collector))
                stack.enter_context(patch.object(friends, "acquire_browser_account_lock", AsyncMock(return_value=(Mock(), "lock", "token"))))
                stack.enter_context(patch.object(friends, "release_browser_account_lock", release))
                stack.enter_context(patch.object(tasks, "_persist_account_send_failure"))
                with CASE.assertRaises(friends.FriendRefreshError) as caught:
                    await friends.fetch_account_friends(user)
                assert caught.exception.category == "browser_cleanup_blocked"
                assert tasks._BLOCKED_BROWSER_LOCKS[friends.account_profile_name(user)][1:] == ("lock", "token")
                assert start_browser.await_count == 1
                assert release.call_count == 0
        finally:
            gate.set()
            await asyncio.wait_for(reader, .2)

    asyncio.run(run())


def test_patrol_does_not_retry_route_after_cleanup_is_blocked():
    async def run():
        user = account()
        inspect_once = AsyncMock(side_effect=friends.FriendRefreshError(
            "uid reader cleanup incomplete", category="browser_cleanup_blocked"
        ))
        with patch.object(patrol, "douyin_network_modes", return_value=["direct", "proxy"]), patch.object(
            patrol, "_inspect_once", inspect_once
        ):
            with CASE.assertRaises(friends.FriendRefreshError) as caught:
                await patrol._inspect_account(user, {"enabled": False})
        assert caught.exception.category == "browser_cleanup_blocked"
        assert inspect_once.await_count == 1

    asyncio.run(run())


def test_patrol_retains_outer_lock_when_uid_cleanup_is_unresolved():
    async def run():
        user = account()
        handle = Mock()
        release = Mock()
        blocked = friends.FriendRefreshError("uid reader cleanup incomplete", category="browser_cleanup_blocked")
        with ExitStack() as stack:
            stack.enter_context(patch.dict(tasks._BLOCKED_BROWSER_LOCKS, {}, clear=True))
            stack.enter_context(patch.object(patrol, "acquire_browser_account_lock", AsyncMock(return_value=(handle, "lock", "token"))))
            stack.enter_context(patch.object(patrol, "release_browser_account_lock", release))
            stack.enter_context(patch.object(patrol, "_inspect_account", AsyncMock(side_effect=blocked)))
            stack.enter_context(patch.object(patrol, "_persist_patrol", Mock()))
            stack.enter_context(patch.object(tasks, "_persist_account_send_failure"))
            result = await patrol.patrol_account(user, profile_config={"enabled": False}, lock_wait=0)
            assert result["login"]["category"] == "browser_cleanup_blocked"
            assert release.call_count == 0
            assert tasks._BLOCKED_BROWSER_LOCKS[friends.account_profile_name(user)] == (handle, "lock", "token")

    asyncio.run(run())


def test_refresh_cancellation_during_close_retains_account_lock():
    async def run():
        user = account()
        page = Resource()
        page.goto = AsyncMock()
        close_started = asyncio.Event()
        async def blocked_close():
            close_started.set()
            await asyncio.Future()
        page.close = AsyncMock(side_effect=blocked_close)
        context = Context(page)
        browser = Resource()
        browser.new_context = AsyncMock(return_value=context)
        playwright = Resource()
        release = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(tasks._BLOCKED_BROWSER_LOCKS, {}, clear=True))
            stack.enter_context(patch.object(friends, 'get_config', return_value={}))
            stack.enter_context(patch.object(friends, 'get_browser', AsyncMock(return_value=(playwright, browser))))
            stack.enter_context(patch.object(friends, 'collect_friend_names', AsyncMock(return_value=friends.FriendScanResult(['friend'], complete=True))))
            stack.enter_context(patch.object(friends.asyncio, 'sleep', AsyncMock()))
            stack.enter_context(patch.object(friends, 'UserDetailCollector', return_value=WaitingCollector()))
            stack.enter_context(patch.object(friends, 'acquire_browser_account_lock', AsyncMock(return_value=(Mock(), 'lock', 'token'))))
            stack.enter_context(patch.object(friends, 'release_browser_account_lock', release))
            persist = stack.enter_context(patch.object(tasks, '_persist_account_send_failure'))
            task = asyncio.create_task(friends._fetch_account_friends_once(user, 'direct'))
            await asyncio.wait_for(close_started.wait(), .2)
            task.cancel()
            with CASE.assertRaises(asyncio.CancelledError):
                await task
            release.assert_not_called()
            assert friends.account_profile_name(user) in tasks._BLOCKED_BROWSER_LOCKS
            assert persist.call_args.args[1] == 'browser_cleanup_blocked'
            context.close.assert_awaited_once()
            browser.close.assert_awaited_once()
            playwright.stop.assert_awaited_once()
    asyncio.run(run())


def test_persisted_cleanup_barrier_blocks_refresh_and_patrol_after_restart():
    user = account()
    user['browser_cleanup_blocked'] = {'category': 'browser_cleanup_blocked'}
    async def run():
        with patch.dict(tasks._BLOCKED_BROWSER_LOCKS, {}, clear=True), \
             patch.object(friends, 'acquire_browser_account_lock', AsyncMock()) as refresh_lock, \
             patch.object(friends, 'get_browser', AsyncMock()) as refresh_browser, \
             patch.object(patrol, 'acquire_browser_account_lock', AsyncMock()) as patrol_lock, \
             patch.object(patrol, '_inspect_account', AsyncMock()) as patrol_browser, \
             patch.object(patrol, '_persist_patrol', Mock()):
            assert friends.browser_account_cleanup_blocked(user)
            with CASE.assertRaises(friends.FriendRefreshError) as caught:
                await friends.fetch_account_friends(user)
            assert caught.exception.category == 'browser_cleanup_blocked'
            result = await patrol.patrol_account(user, profile_config={'enabled': False}, lock_wait=0)
            assert result['skipped']
            assert result['login']['category'] == 'browser_cleanup_blocked'
            refresh_lock.assert_not_awaited()
            refresh_browser.assert_not_awaited()
            patrol_lock.assert_not_awaited()
            patrol_browser.assert_not_awaited()
    asyncio.run(run())


def test_login_uid_watcher_close_cancels_cooperative_body():
    async def run():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def body():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        watcher = patrol.LoginUidWatcher()
        reader = asyncio.create_task(body())
        watcher._tasks.add(reader)
        await asyncio.wait_for(started.wait(), .2)
        try:
            assert await watcher.aclose(), 'a cooperative body should exit, not permanently block the account'
            assert cancelled.is_set()
        finally:
            reader.cancel()
            await asyncio.wait({reader}, timeout=.2)
    asyncio.run(run())


def test_shared_cleanup_retains_pending_reader_references():
    async def run():
        gate = asyncio.Event()
        async def body():
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    continue
        collector = WaitingCollector(complete=False)
        reader = asyncio.create_task(body())
        collector._pending.add(reader)
        await asyncio.sleep(0)
        try:
            complete, cancelled = await friends.close_uid_collectors(collector)
            assert not complete
            assert not cancelled
            assert reader in tasks._PENDING_BROWSER_TASKS, 'pending body must stay strongly referenced'
        finally:
            gate.set()
            await asyncio.wait({reader}, timeout=.2)
    asyncio.run(run())


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for test in (
        test_refresh_cancellation_during_close_retains_account_lock,
        test_persisted_cleanup_barrier_blocks_refresh_and_patrol_after_restart,
        test_login_uid_watcher_close_cancels_cooperative_body,
        test_shared_cleanup_retains_pending_reader_references,
        test_friend_refresh_cancellation_closes_resources_and_releases_lock,
        test_friend_refresh_stubborn_reader_blocks_account_and_stops_route_retry,
        test_patrol_does_not_retry_route_after_cleanup_is_blocked,
        test_patrol_retains_outer_lock_when_uid_cleanup_is_unresolved,
    ):
        suite.addTest(unittest.FunctionTestCase(test))
    return suite
