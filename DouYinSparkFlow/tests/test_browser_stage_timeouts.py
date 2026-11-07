"""Offline browser-stage regression tests; no browser or account connections."""
import asyncio
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import unittest

from core import tasks, streak_state


@contextmanager
def flow(*, identity_guard=False):
    user = {"username": "synthetic", "unique_id": "synthetic-timeouts",
            "cookies": [], "targets": ["first", "second", "third"]}
    page = Mock(goto=AsyncMock(), close=AsyncMock())
    context = Mock(new_page=AsyncMock(return_value=page), add_cookies=AsyncMock(),
                   cookies=AsyncMock(return_value=[]), close=AsyncMock())
    browser = Mock(new_context=AsyncMock(return_value=context))
    chat = Mock(type=AsyncMock(), press=AsyncMock())
    collector = Mock(aclose=AsyncMock(return_value=True), pending_tasks=Mock(return_value=()))
    collector.attach.return_value = collector
    release = Mock()
    strategy = {"shuffleTargets": False, "accountStartDelaySecondsMin": 0,
                "accountStartDelaySecondsMax": 0, "messageIntervalSecondsMin": 0,
                "messageIntervalSecondsMax": 0}

    async def selected(*args, **kwargs):
        for target in args[3]:
            yield target

    async def no_sleep(*args, **kwargs):
        pass

    async def run(**kwargs):
        await tasks.do_user_task(browser, user, asyncio.Semaphore(1), strategy,
                                 {"enabled": False}, {"maxScanSeconds": 1},
                                 "direct", run_id="offline-timeout", **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.dict(tasks._BLOCKED_BROWSER_LOCKS, {}, clear=True))
        # The real profile helper reloads the durable row; use this test's store.
        from core import profile_cookies
        stack.enter_context(patch.object(profile_cookies, 'get_userData', lambda **kw: [user]))
        stack.enter_context(patch.object(profile_cookies, 'update_user_data', lambda mutate, **kw: mutate([user])[0]))
        patches = {
            "_acquire_browser_account_lock": AsyncMock(return_value=(Mock(), Mock(), "token")),
            "_release_browser_account_lock": release,
            "update_user_data": lambda mutate, **kw: mutate([user])[0],
            "get_userData": lambda **kw: [user],
            "_sleep_with_log": AsyncMock(),
            "ensure_not_login_required": AsyncMock(),
            "_page_has_login_required_prompt": AsyncMock(return_value=(False, "")),
            "scroll_and_select_user": selected,
            "locate_chat_input": AsyncMock(return_value=(chat, "input")),
            "snapshot_last_own_message": AsyncMock(return_value=None),
            "build_message": Mock(return_value="offline message"),
            "save_debug_artifacts": AsyncMock(),
            "_detect_send_failure_indicator": AsyncMock(return_value=""),
            "start_im_send_observer": AsyncMock(return_value=AsyncMock(return_value={"enabled": False})),
            "confirm_message_sent": AsyncMock(return_value=(True, "confirmed")),
        }
        if not identity_guard:
            # These fixtures test timeout lifecycles, not DOM recipient checks.
            patches['_require_friend_chat'] = AsyncMock()
            stack.enter_context(patch.object(tasks.friend_chat_guard, 'dispatch_was_allowed',
                                             AsyncMock(return_value=True)))
        for name, value in patches.items():
            stack.enter_context(patch.object(tasks, name, value))
        stack.enter_context(patch.object(tasks.friend_identity, "UserDetailCollector", return_value=collector))
        # Avoid wall-clock waits without changing asyncio.wait deadline behavior.
        stack.enter_context(patch.object(tasks.asyncio, "sleep", no_sleep))
        yield user, page, context, browser, chat, release, run


async def hangs(*args, **kwargs):
    await asyncio.Future()


def test_stage_wrapper_is_bounded_even_if_cancellation_is_ignored():
    async def exercise():
        gate = asyncio.Event()

        async def stubborn():
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await gate.wait()

        started = asyncio.get_running_loop().time()
        with unittest.TestCase().assertRaises(tasks.BrowserStageTimeout) as caught:
            await tasks._run_browser_stage("login_check", stubborn(), timeout_seconds=.01,
                                           cancel_grace_seconds=.01)
        assert caught.exception.stage == "login_check"
        assert caught.exception.category == "browser_stage_timeout"
        assert asyncio.get_running_loop().time() - started < .5
        await asyncio.sleep(0)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    asyncio.run(exercise())


def test_login_probe_count_is_enclosed_despite_inner_exception_handlers():
    page = Mock()
    page.locator.return_value.first.count = AsyncMock(side_effect=hangs)
    async def exercise():
        with patch.object(tasks, "BROWSER_STAGE_TIMEOUT_SECONDS", .01):
            with unittest.TestCase().assertRaises(tasks.BrowserStageTimeout):
                await tasks._page_has_login_required_prompt(page)
    asyncio.run(exercise())


def test_before_send_timeout_is_one_account_fault_then_cooldown(flow):
    user, page, context, browser, chat, release, run = flow
    with patch.object(tasks, "BROWSER_STAGE_TIMEOUT_SECONDS", .01, create=True), \
         patch.object(tasks, "ensure_not_login_required", AsyncMock(side_effect=hangs)), \
         patch.object(tasks, "_persist_browser_send_failure") as failed:
        asyncio.run(run())
    chat.press.assert_not_awaited()
    failed.assert_not_called()
    assert user["account_failure"]["category"] == "browser_stage_timeout"
    assert user["account_failure"]["affectedTargets"] == user["targets"]
    assert user["account_failure"]["attemptCount"] == 1
    now = datetime.now(timezone.utc)
    assert tasks._account_paused_by_failure_today(user, now)
    assert not tasks._account_paused_by_failure_today(user, now + timedelta(hours=2))
    context.close.assert_awaited_once()
    release.assert_called_once()


def test_whole_account_timeout_preserves_dispatched_and_confirmed_targets(flow):
    user, page, context, browser, chat, release, run = flow
    original = tasks._persist_browser_send_success

    def success(*args, **kwargs):
        # Exercise real persistence with a strong synthetic receipt.
        kwargs["server_receipt"] = {"ok": True, "protoParsed": True, "protoOk": True,
                                    "httpStatus": 200, "call": "message_send",
                                    "serverMessageId": "offline-receipt"}
        return original(*args, **kwargs)

    async def press(key):
        if key == "Enter" and chat.press.await_count == 2:
            await hangs()

    # Allow the fixture to reach the intentional second-Enter hang even
    # under CPU throttling; the production deadline is not changed.
    with patch.object(tasks, "_browser_account_timeout_seconds", return_value=1.0), \
         patch.object(tasks, "_persist_browser_send_success", side_effect=success), \
         patch.object(tasks, "_persist_browser_send_failure") as failed:
        chat.press.side_effect = press
        asyncio.run(run())
    assert chat.press.await_count == 2, "the deliberate second-Enter hang must be reached"
    failed.assert_not_called()
    now = datetime.now(tasks._schedule_timezone())
    assert tasks._target_sent_today(user, "first", now)
    assert streak_state.attempt_in_progress(user, "second", now) or streak_state.is_sent_unverified(user, "second", now)
    assert user["account_failure"]["affectedTargets"] == ["third"]
    due, *_ = tasks._select_due_targets(user, {"enabled": True, "startHour": 0, "endHour": 24}, now)
    assert "second" not in due
    later = now + timedelta(hours=2)
    due_later, *_ = tasks._select_due_targets(user, {"enabled": True, "startHour": 0, "endHour": 24}, later)
    assert "second" not in due_later
    release.assert_called_once()


def test_new_page_timeout_also_records_account_fault(flow):
    user, page, context, browser, chat, release, run = flow
    context.new_page.side_effect = hangs
    with patch.object(tasks, "BROWSER_STAGE_TIMEOUT_SECONDS", .01, create=True):
        asyncio.run(run())
    assert user["account_failure"]["category"] == "browser_stage_timeout"
    chat.press.assert_not_awaited()
    release.assert_called_once()


def test_cancellation_cleans_up_and_reraises(flow):
    user, page, context, browser, chat, release, run = flow
    async def exercise():
        entered = asyncio.Event()
        async def login(*args):
            entered.set()
            await hangs()
        with patch.object(tasks, "ensure_not_login_required", side_effect=login):
            task = asyncio.create_task(run())
            await entered.wait()
            task.cancel()
            with unittest.TestCase().assertRaises(asyncio.CancelledError):
                await task
    asyncio.run(exercise())
    context.close.assert_awaited_once()
    release.assert_called_once()


def test_unresponsive_cleanup_retains_lock_and_stops_owned_driver(flow):
    user, page, context, browser, chat, release, run = flow
    async def stubborn(*args):
        try:
            await hangs()
        except asyncio.CancelledError:
            await hangs()
    context.close.side_effect = stubborn
    page.close.side_effect = stubborn
    driver = Mock(stop=AsyncMock())
    profile = {"enabled": True, "root": "offline", "syncStoredCookiesBeforeRun": False,
               "seedCookiesWhenEmpty": False, "refreshStoredCookiesAfterLogin": False}
    async def exercise():
        with patch.object(tasks, "get_persistent_browser_context", AsyncMock(return_value=(driver, context, "offline"))), \
             patch.object(tasks, "BROWSER_CLEANUP_TIMEOUT_SECONDS", .01, create=True), \
             patch.object(tasks, "ensure_not_login_required", side_effect=RuntimeError("login_required")):
            await tasks.do_user_task(None, user, asyncio.Semaphore(1),
                {"shuffleTargets": False, "accountStartDelaySecondsMin": 0, "accountStartDelaySecondsMax": 0},
                profile, {"maxScanSeconds": 1}, "direct", run_id="cleanup")
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(exercise())
    driver.stop.assert_awaited_once()
    release.assert_not_called()
    assert user["account_failure"]["category"] == "browser_cleanup_blocked"


def test_prelaunch_guard_skips_existing_inflight(flow):
    user, page, context, browser, chat, release, run = flow
    now = datetime.now(timezone.utc)
    for target in user["targets"]:
        streak_state.mark_in_flight(user, target, run_id="previous", strategy="browser", now=now)
    asyncio.run(run())
    browser.new_context.assert_not_awaited()
    chat.press.assert_not_awaited()


def test_cleanup_block_survives_day_boundary_and_manual_retry(flow):
    user, page, context, browser, chat, release, run = flow
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    user["account_failure"] = {"category": "browser_cleanup_blocked", "attemptCount": 1,
                               "lastAttemptAt": yesterday.isoformat()}
    assert tasks._account_paused_by_failure_today(user, datetime.now(timezone.utc))
    asyncio.run(run(allow_retry=True))
    browser.new_context.assert_not_awaited()


def test_navigation_envelope_cancels_retry_loop_without_send(flow):
    user, page, context, browser, chat, release, run = flow
    original = tasks._run_browser_stage
    async def fast_navigation(stage, operation, **kwargs):
        if stage == "navigation":
            kwargs["timeout_seconds"] = .01
        return await original(stage, operation, **kwargs)
    page.goto.side_effect = hangs
    with patch.object(tasks, "_run_browser_stage", side_effect=fast_navigation):
        asyncio.run(run())
    assert user["account_failure"]["category"] == "browser_stage_timeout"
    page.goto.assert_awaited_once()
    chat.press.assert_not_awaited()
    release.assert_called_once()


def test_cookie_probe_timeouts_cleanup_owned_session(flow):
    user, page, context, browser, chat, release, run = flow
    driver = Mock(stop=AsyncMock())
    strategy = {"shuffleTargets": False, "accountStartDelaySecondsMin": 0,
                "accountStartDelaySecondsMax": 0}
    async def exercise():
        for seed in (True, False):
            user.pop("account_failure", None)
            context.cookies = AsyncMock(side_effect=hangs)
            profile = {"enabled": True, "root": "offline", "syncStoredCookiesBeforeRun": False,
                       "seedCookiesWhenEmpty": seed, "refreshStoredCookiesAfterLogin": not seed}
            user["cookies"] = [{"name": "offline", "value": "not-a-real-cookie"}]
            with patch.object(tasks, "get_persistent_browser_context", AsyncMock(return_value=(driver, context, "offline"))), \
                 patch.object(tasks, "BROWSER_STAGE_TIMEOUT_SECONDS", .01):
                await tasks.do_user_task(None, user, asyncio.Semaphore(1), strategy, profile,
                                         {"maxScanSeconds": 1}, "direct", run_id="offline-cookies")
            assert user["account_failure"]["category"] == "browser_stage_timeout"
            assert user["account_failure"]["attemptCount"] == 1
    asyncio.run(exercise())
    assert driver.stop.await_count == 2
    assert release.call_count == 2
    chat.press.assert_not_awaited()


def test_unresponsive_driver_after_confirmed_context_close_releases_lock(flow):
    user, page, context, browser, chat, release, run = flow
    async def stubborn():
        try:
            await hangs()
        except asyncio.CancelledError:
            await hangs()
    driver = Mock(stop=AsyncMock(side_effect=stubborn))
    profile = {"enabled": True, "root": "offline", "syncStoredCookiesBeforeRun": False,
               "seedCookiesWhenEmpty": False, "refreshStoredCookiesAfterLogin": False}
    async def exercise():
        with patch.object(tasks, "get_persistent_browser_context", AsyncMock(return_value=(driver, context, "offline"))), \
             patch.object(tasks, "BROWSER_CLEANUP_TIMEOUT_SECONDS", .01), \
             patch.object(tasks, "ensure_not_login_required", side_effect=RuntimeError("login_required")):
            await tasks.do_user_task(None, user, asyncio.Semaphore(1),
                {"shuffleTargets": False, "accountStartDelaySecondsMin": 0, "accountStartDelaySecondsMax": 0},
                profile, {"maxScanSeconds": 1}, "direct", run_id="offline-close")
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(exercise())
    context.close.assert_awaited_once()
    release.assert_called_once()


def test_prelaunch_terminal_and_used_quota_guards(flow):
    from core import refusal_retry
    user, page, context, browser, chat, release, run = flow
    now = datetime.now(tasks._schedule_timezone())
    streak_state.mark_failed(user, "first", category="group_identity_unavailable", reason="offline", retryable=False, now=now)
    with patch.object(refusal_retry, "used_today", side_effect=lambda account, target, at, schedule_timezone=None: target == "second"), \
         patch.object(refusal_retry, "eligible", return_value=False):
        assert tasks._browser_prelaunch_targets(user, user["targets"], False) == ["third"]
        assert tasks._browser_prelaunch_targets(user, user["targets"], True) == user["targets"]


def test_manual_allow_retry_keeps_unknown_override_semantics(flow):
    user, *_ = flow
    now = datetime.now(tasks._schedule_timezone())
    streak_state.mark_sent_unverified(user, "first", now=now, detail="pre_enter_outcome_unknown_barrier")
    assert "first" not in tasks._browser_prelaunch_targets(user, user["targets"], False)
    assert "first" in tasks._browser_prelaunch_targets(user, user["targets"], True)


def test_nested_login_timeout_drains_cancellation_resistant_probe():
    async def exercise():
        async def stubborn():
            try:
                await hangs()
            except asyncio.CancelledError:
                await hangs()
        page = Mock()
        page.locator.return_value.first.count = AsyncMock(side_effect=stubborn)
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
        with patch.object(tasks, "BROWSER_STAGE_TIMEOUT_SECONDS", .01):
            with unittest.TestCase().assertRaises(tasks.BrowserStageTimeout):
                await tasks._run_browser_stage("login_check", tasks.ensure_not_login_required(page, "synthetic", "offline"))
        await asyncio.sleep(0)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert not errors
    asyncio.run(exercise())


def test_direct_last_message_read_is_bounded():
    async def exercise():
        page = Mock(evaluate=AsyncMock(side_effect=hangs))
        with patch.object(tasks, "BROWSER_STAGE_TIMEOUT_SECONDS", .01):
            with unittest.TestCase().assertRaises(tasks.BrowserStageTimeout):
                await tasks.snapshot_last_own_message(page)
    # An outer watchdog makes the pre-fix test fail instead of waiting forever.
    async def watchdog():
        await asyncio.wait_for(exercise(), .2)
    asyncio.run(watchdog())


def test_cancel_during_cleanup_still_closes_context(flow):
    user, page, context, browser, chat, release, run = flow
    async def exercise():
        closing = asyncio.Event()
        async def close():
            closing.set()
            await hangs()
        page.close.side_effect = close
        with patch.object(tasks, "ensure_not_login_required", side_effect=RuntimeError("login_required")):
            task = asyncio.create_task(run())
            await closing.wait()
            task.cancel()
            with unittest.TestCase().assertRaises(asyncio.CancelledError):
                await task
    asyncio.run(exercise())
    context.close.assert_awaited_once()
    release.assert_called_once()


def test_last_message_and_input_timeouts_are_account_faults(flow):
    user, page, context, browser, chat, release, run = flow
    for function in ("locate_chat_input", "snapshot_last_own_message"):
        user.pop("account_failure", None)
        with patch.object(tasks, "BROWSER_STAGE_TIMEOUT_SECONDS", .01), \
             patch.object(tasks, function, AsyncMock(side_effect=hangs)):
            asyncio.run(run())
        assert user["account_failure"]["category"] == "browser_stage_timeout"
        assert user["account_failure"]["affectedTargets"] == user["targets"]
    chat.press.assert_not_awaited()


def test_scan_deadline_excludes_delivery_time():
    async def exercise():
        async def selected():
            yield "first"
            yield "second"
        found = []
        async for target in tasks._bounded_target_scan(selected(), .01):
            found.append(target)
            await asyncio.sleep(.02)
        assert found == ["first", "second"]
        async def wedged():
            await hangs()
            yield "never"
        with unittest.TestCase().assertRaises(tasks.BrowserStageTimeout):
            await tasks._bounded_target_scan(wedged(), .01).__anext__()
    asyncio.run(exercise())


def test_group_clear_timeout_does_not_enter_recovery_or_next_target(flow):
    user, page, context, browser, chat, release, run = flow
    user['targets'] = ['群聊:demo', 'later']
    chat.fill = AsyncMock(side_effect=hangs)
    with patch.object(tasks, 'BROWSER_STAGE_TIMEOUT_SECONDS', .01), \
         patch.object(tasks, '_browser_account_timeout_seconds', return_value=.5), \
         patch.object(tasks, 'get_bound_group_conversation_id', return_value='7691234567890123400'), \
         patch.object(tasks, '_verify_open_group_conversation', AsyncMock(return_value=(False, 'offline mismatch'))), \
         patch.object(tasks, 'detect_message_already_sent', AsyncMock(return_value=(False, 'none'))) as recovery:
        asyncio.run(run())
    recovery.assert_not_awaited()
    chat.press.assert_not_awaited()
    assert 'stage=clear_input' in user['account_failure']['reason']


def test_enter_timeout_uses_local_budget_and_keeps_unknown(flow):
    user, page, context, browser, chat, release, run = flow
    async def press(key):
        if key == 'Enter':
            await hangs()
    chat.press.side_effect = press
    with patch.object(tasks, 'BROWSER_STAGE_TIMEOUT_SECONDS', .01), \
         patch.object(tasks, '_browser_account_timeout_seconds', return_value=.3):
        asyncio.run(run())
    assert 'stage=dispatch_enter' in user['account_failure']['reason']
    now = datetime.now(timezone.utc)
    assert streak_state.is_sent_unverified(user, 'first', now)
    assert chat.press.await_count == 1
    release.assert_called_once()


def test_all_cleanup_stages_stubborn_keep_owned_tasks_and_lock(flow):
    user, page, context, browser, chat, release, run = flow
    driver = Mock(stop=AsyncMock())
    profile = {'enabled': True, 'root': 'offline', 'syncStoredCookiesBeforeRun': False,
               'seedCookiesWhenEmpty': False, 'refreshStoredCookiesAfterLogin': False}
    async def exercise():
        gate = asyncio.Event()
        async def stubborn(*args):
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    continue
        page.close.side_effect = stubborn
        context.close.side_effect = stubborn
        driver.stop.side_effect = stubborn
        try:
            with patch.object(tasks, 'get_persistent_browser_context', AsyncMock(return_value=(driver, context, 'offline'))), \
                 patch.object(tasks, 'BROWSER_CLEANUP_TIMEOUT_SECONDS', .01), \
                 patch.object(tasks, '_browser_account_timeout_seconds', return_value=.02), \
                 patch.object(tasks, 'ensure_not_login_required', side_effect=hangs):
                await tasks.do_user_task(None, user, asyncio.Semaphore(1),
                    {'shuffleTargets': False, 'accountStartDelaySecondsMin': 0, 'accountStartDelaySecondsMax': 0},
                    profile, {'maxScanSeconds': 1}, 'direct', run_id='cleanup-all')
            assert user['account_failure']['category'] == 'browser_cleanup_blocked'
            release.assert_not_called()
            assert any(not task.done() for task in tasks._PENDING_BROWSER_TASKS)
        finally:
            gate.set()
            pending = [t for t in tasks._PENDING_BROWSER_TASKS if not t.done()]
            if pending:
                await asyncio.wait(pending, timeout=.2)
            tasks._BLOCKED_BROWSER_LOCKS.clear()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(exercise())


def test_uncooperative_operation_is_retained_not_forcibly_closed():
    async def exercise():
        gate = asyncio.Event()
        exited = False
        async def stubborn():
            nonlocal exited
            try:
                while not gate.is_set():
                    try:
                        await gate.wait()
                    except asyncio.CancelledError:
                        continue
            finally:
                exited = True
        try:
            with unittest.TestCase().assertRaises(tasks.BrowserStageTimeout):
                await tasks._run_browser_stage('offline_stubborn', stubborn(), timeout_seconds=.01, cancel_grace_seconds=.01)
            assert not exited, 'must not forcibly close a running coroutine and pretend it completed'
            assert any(not t.done() for t in tasks._PENDING_BROWSER_TASKS)
        finally:
            gate.set()
            await asyncio.sleep(.03)
    asyncio.run(exercise())


class BrowserStageTimeoutTests(unittest.TestCase):
    pass


def _case(function):
    def run(self):
        if function.__code__.co_argcount:
            with flow() as fixture:
                function(fixture)
        else:
            function()
    return run


for _name, _function in list(globals().items()):
    if _name.startswith("test_"):
        setattr(BrowserStageTimeoutTests, _name, _case(_function))
        # The unittest adapter owns collection; do not expose fixture-taking
        # helper functions as a second, incompatible set of pytest tests.
        del globals()[_name]
