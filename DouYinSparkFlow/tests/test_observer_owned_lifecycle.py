"""Offline CDP observer lifecycle regressions; no browser, profile, or network."""
import asyncio
import contextvars
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core import tasks


class _Session:
    def __init__(self, *, stubborn=False):
        self.handlers = {}
        self.body_started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release_body = asyncio.Event()
        self.stubborn = stubborn
        self.reader_task = None
        self.detach_calls = 0

    def on(self, event, callback):
        self.handlers[event] = callback

    async def send(self, method, params=None):
        if method != "Network.getResponseBody":
            return {}
        self.reader_task = asyncio.current_task()
        self.body_started.set()
        while not self.release_body.is_set():
            try:
                await self.release_body.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                if not self.stubborn:
                    raise
        return {"body": json.dumps({"status_code": 0}), "base64Encoded": False}

    async def detach(self):
        self.detach_calls += 1


class ObserverOwnedLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = _Session()
        self.page = MagicMock()

        async def new_cdp_session(_page):
            return self.session

        self.page.context.new_cdp_session = new_cdp_session
        self.owner = set()
        self.owner_token = tasks._BROWSER_OPERATION_OWNER.set(self.owner)

    async def asyncTearDown(self):
        tasks._BROWSER_OPERATION_OWNER.reset(self.owner_token)
        self.session.release_body.set()
        if self.session.reader_task is not None:
            await asyncio.wait_for(self.session.reader_task, timeout=.5)

    def _emit_send_response(self, request_id="receipt"):
        url = "https://imapi.douyin.com/v1/message/send?aid=offline"
        self.session.handlers["Network.requestWillBeSent"]({
            "requestId": request_id,
            "request": {"url": url, "method": "POST", "postData": "offline"},
        })
        self.session.handlers["Network.responseReceived"]({
            "requestId": request_id,
            "response": {"status": 200, "headers": {}},
        })
        self.session.handlers["Network.loadingFinished"]({"requestId": request_id})

    def _emit_send_response_from_empty_context(self, request_id="receipt"):
        """CDP dispatch can run outside the task that created the observer."""
        contextvars.Context().run(self._emit_send_response, request_id)

    async def test_disabled_observer_returns_independent_nested_results(self):
        async def unavailable(_page):
            raise RuntimeError("synthetic CDP unavailable")
        self.page.context.new_cdp_session = unavailable
        summary = await tasks.start_im_send_observer(self.page, "synthetic", "target")
        first = await summary()
        first["events"].append({"kind": "caller mutation"})
        second = await summary()
        self.assertEqual([], second["events"])
        self.assertIsNot(first["events"], second["events"])

    async def test_slow_body_inside_summary_budget_is_owned_and_adopted(self):
        summary = await tasks.start_im_send_observer(self.page, "synthetic", "target")
        self._emit_send_response()
        await asyncio.wait_for(self.session.body_started.wait(), timeout=.5)

        self.assertIn(self.session.reader_task, self.owner)
        self.assertIn(self.session.reader_task, tasks._PENDING_BROWSER_TASKS)
        asyncio.get_running_loop().call_later(.01, self.session.release_body.set)

        result = await summary(extra_wait_seconds=.05)

        self.assertTrue(result["send_receipt"]["ok"])
        self.assertNotIn("pendingBody", result["send_receipt"])
        self.assertEqual(1, self.session.detach_calls)

    async def test_summary_waits_for_late_receipt_inside_existing_budget(self):
        summary = await tasks.start_im_send_observer(self.page, "synthetic", "target")

        result_task = asyncio.create_task(summary(extra_wait_seconds=.05))
        await asyncio.sleep(.01)
        self.assertFalse(result_task.done(), "summary must retain its existing receipt budget")
        self._emit_send_response("late")
        asyncio.get_running_loop().call_later(.001, self.session.release_body.set)

        result = await result_task

        self.assertTrue(result["send_response_seen"])
        self.assertTrue(result["send_receipt"]["ok"])
        self.assertEqual(1, self.session.detach_calls)

    async def test_cdp_callback_from_empty_context_keeps_creator_owner(self):
        summary = await tasks.start_im_send_observer(self.page, "synthetic", "target")
        self._emit_send_response_from_empty_context("foreign-context")
        await asyncio.wait_for(self.session.body_started.wait(), timeout=.5)

        self.assertIn(self.session.reader_task, self.owner)
        self.session.release_body.set()
        result = await summary(extra_wait_seconds=.05)

        self.assertTrue(result["send_receipt"]["ok"])

    async def test_do_user_task_blocks_lock_for_uncooperative_observer_reader(self):
        self.session.stubborn = True
        user = {"username": "synthetic", "unique_id": "outer-owner", "cookies": [], "targets": ["target"]}
        lock = MagicMock()
        failures = []

        async def locked_attempt(*_args, **_kwargs):
            summary = await tasks.start_im_send_observer(self.page, "synthetic", "target")
            self._emit_send_response_from_empty_context("outer-reader")
            await asyncio.wait_for(self.session.body_started.wait(), timeout=.5)
            await summary(extra_wait_seconds=.01)

        def record_failure(_user, category, *_args, **_kwargs):
            failures.append(category)

        lock_key = tasks._account_profile_name(user)
        with patch.dict(tasks._BLOCKED_BROWSER_LOCKS, {}, clear=True), \
             patch.object(tasks, "_acquire_browser_account_lock", AsyncMock(return_value=(lock, "path", "token"))), \
             patch.object(tasks, "_release_browser_account_lock") as release, \
             patch.object(tasks, "_browser_account_timeout_seconds", return_value=.2), \
             patch.object(tasks, "_do_user_task_locked", new=locked_attempt), \
             patch.object(tasks, "_persist_account_send_failure", side_effect=record_failure):
            await tasks.do_user_task(
                None, user, asyncio.Semaphore(1),
                {"shuffleTargets": False, "accountStartDelaySecondsMin": 0,
                 "accountStartDelaySecondsMax": 0},
                {"enabled": False}, {"maxScanSeconds": 1}, "direct", run_id="outer-observer",
            )
            release.assert_not_called()
            self.assertIn(lock_key, tasks._BLOCKED_BROWSER_LOCKS)
            self.assertIn("browser_cleanup_blocked", failures)

        self.assertFalse(self.session.reader_task.done())

    async def test_cancelled_summary_cancels_reader_but_retains_uncooperative_owner(self):
        self.session.stubborn = True
        summary = await tasks.start_im_send_observer(self.page, "synthetic", "target")
        self._emit_send_response("cancel")
        await asyncio.wait_for(self.session.body_started.wait(), timeout=.5)

        running = asyncio.create_task(summary(extra_wait_seconds=1))
        await asyncio.sleep(0)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running

        await asyncio.wait_for(self.session.cancel_seen.wait(), timeout=.5)
        self.assertFalse(self.session.reader_task.done())
        self.assertIn(self.session.reader_task, self.owner)
        self.assertIn(self.session.reader_task, tasks._PENDING_BROWSER_TASKS)

    async def test_timed_out_summary_is_idempotent_and_rejects_late_body_update(self):
        self.session.stubborn = True
        summary = await tasks.start_im_send_observer(self.page, "synthetic", "target")
        self._emit_send_response("timeout")
        await asyncio.wait_for(self.session.body_started.wait(), timeout=.5)

        first = await summary(extra_wait_seconds=.01)
        first_snapshot = json.dumps(first, sort_keys=True)
        self.assertTrue(first["send_receipt"]["pendingBody"])
        self.assertIn(self.session.reader_task, self.owner)
        self.assertIn(self.session.reader_task, tasks._PENDING_BROWSER_TASKS)

        self.session.release_body.set()
        await asyncio.wait_for(self.session.reader_task, timeout=.5)
        self.assertEqual(first_snapshot, json.dumps(first, sort_keys=True))
        second = await summary(extra_wait_seconds=.1)
        self.assertEqual(first, second)
        self.assertTrue(second["send_receipt"]["pendingBody"])
        first["events"].append({"kind": "synthetic caller mutation"})
        third = await summary(extra_wait_seconds=0)
        self.assertEqual(first_snapshot, json.dumps(third, sort_keys=True))
