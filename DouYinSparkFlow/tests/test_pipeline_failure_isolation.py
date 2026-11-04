"""Offline pipeline isolation regressions; no browser or account connections."""
import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from core import streak_state, tasks
from tests.test_browser_stage_timeouts import flow, hangs


def _strong_receipt():
    return {
        "ok": True,
        "protoOk": True,
        "httpStatus": 200,
        "call": "message_send",
        "serverMessageId": "synthetic-receipt",
    }


class PipelineFailureIsolationTests(unittest.TestCase):
    def test_confirmed_a_then_explicit_8610_b_continues_to_c(self):
        with flow() as (user, page, context, browser, chat, release, run):
            async def observer(_page, _account, target):
                receipt = _strong_receipt()
                if target == "second":
                    receipt = {
                        "ok": False,
                        "call": "message_send",
                        "rejection": {"key": "status_code", "value": 8610, "source": "raw"},
                    }
                return AsyncMock(return_value={
                    "enabled": True,
                    "send_request_seen": True,
                    "send_response_seen": True,
                    "send_receipt": receipt,
                })

            with patch.object(tasks, "start_im_send_observer", side_effect=observer):
                asyncio.run(run())

        now = datetime.now(tasks._schedule_timezone())
        self.assertTrue(tasks._target_sent_today(user, "first", now))
        self.assertTrue(tasks._target_sent_today(user, "third", now))
        self.assertEqual(user["failure_queue"]["second"]["category"], "send_rejected")
        self.assertEqual(chat.press.await_count, 3)
        self.assertEqual(release.call_count, 1)

    def test_pre_dispatch_hang_closes_session_without_target_failures_for_c(self):
        with flow() as (user, page, context, browser, chat, release, run):
            snapshots = 0

            async def snapshot(*_args, **_kwargs):
                nonlocal snapshots
                snapshots += 1
                if snapshots == 2:
                    await hangs()
                return None

            with patch.object(tasks, "BROWSER_STAGE_TIMEOUT_SECONDS", .01), \
                 patch.object(tasks, "snapshot_last_own_message", side_effect=snapshot), \
                 patch.object(tasks, "start_im_send_observer", AsyncMock(return_value=AsyncMock(return_value={
                     "enabled": True,
                     "send_request_seen": True,
                     "send_response_seen": True,
                     "send_receipt": _strong_receipt(),
                 }))):
                asyncio.run(run())

        now = datetime.now(tasks._schedule_timezone())
        self.assertTrue(tasks._target_sent_today(user, "first", now))
        self.assertFalse(tasks._target_sent_today(user, "third", now))
        self.assertFalse(streak_state.attempt_in_progress(user, "third", now))
        self.assertFalse(streak_state.is_sent_unverified(user, "third", now))
        self.assertNotIn("third", user.get("failure_queue") or {})
        self.assertNotIn("third", user.get("message_history") or {})
        self.assertEqual(chat.press.await_count, 1)
        self.assertEqual(user["account_failure"]["category"], "browser_stage_timeout")
        self.assertEqual(user["account_failure"]["affectedTargets"], ["second", "third"])
        context.close.assert_awaited_once()
        release.assert_called_once()

    def test_after_enter_receipt_hang_keeps_b_unknown_and_c_unattempted(self):
        with flow() as (user, page, context, browser, chat, release, run):
            confirmations = 0
            async def confirm(*_args, **_kwargs):
                nonlocal confirmations
                confirmations += 1
                if confirmations == 2:
                    await hangs()
                return True, 'synthetic confirmed'
            with patch.object(tasks, "BROWSER_STAGE_TIMEOUT_SECONDS", .02), \
                 patch.object(tasks, "confirm_message_sent", side_effect=confirm), \
                 patch.object(tasks, "start_im_send_observer", AsyncMock(return_value=AsyncMock(return_value={
                     "enabled": True, "send_request_seen": True, "send_response_seen": True,
                     "send_receipt": _strong_receipt(),
                 }))):
                asyncio.run(run())
        now = datetime.now(tasks._schedule_timezone())
        self.assertTrue(tasks._target_sent_today(user, 'first', now))
        self.assertTrue(streak_state.is_sent_unverified(user, 'second', now))
        self.assertFalse(streak_state.is_sent_unverified(user, 'third', now))
        self.assertNotIn('third', user.get('failure_queue') or {})
        self.assertEqual(tasks._browser_prelaunch_targets(user, user['targets'], False), ['third'])
        self.assertEqual(chat.press.await_count, 2)
        release.assert_called_once()

    def test_sender_awaits_collector_close_before_releasing_lock(self):
        with flow() as (user, page, context, browser, chat, release, run):
            collector = tasks.friend_identity.UserDetailCollector.return_value
            collector.pending_tasks.return_value = ()
            collector.aclose = AsyncMock(return_value=True)
            asyncio.run(run())
            collector.aclose.assert_awaited_once()
            release.assert_called_once()

    def test_uncooperative_uid_body_retains_account_lock(self):
        async def exercise():
            gate = asyncio.Event()
            async def body():
                while not gate.is_set():
                    try:
                        await gate.wait()
                    except asyncio.CancelledError:
                        continue
            pending = asyncio.create_task(body())
            await asyncio.sleep(0)
            try:
                with flow() as (user, page, context, browser, chat, release, run):
                    collector = tasks.friend_identity.UserDetailCollector.return_value
                    collector.pending_tasks.return_value = (pending,)
                    collector.aclose = AsyncMock(return_value=False)
                    await run()
                    release.assert_not_called()
                    self.assertEqual(user["account_failure"]["category"], "browser_cleanup_blocked")
                    self.assertIn(pending, tasks._PENDING_BROWSER_TASKS)
            finally:
                gate.set()
                await asyncio.wait({pending}, timeout=.2)
                tasks._BLOCKED_BROWSER_LOCKS.clear()
        asyncio.run(exercise())

    def test_identity_mismatch_stops_account_and_bounds_exceptional_disarm(self):
        from core import friend_chat_guard

        with flow(identity_guard=True) as (user, page, context, browser, chat, release, run):
            async def mismatch(*_args, **_kwargs):
                return False, "friend_active_identity_mismatch"

            with patch.object(friend_chat_guard, "verify_friend", side_effect=mismatch), \
                 patch.object(friend_chat_guard, "disarm", side_effect=hangs), \
                 patch.object(tasks, "BROWSER_CLEANUP_TIMEOUT_SECONDS", .01):
                asyncio.run(asyncio.wait_for(run(), timeout=.2))

        self.assertEqual(user["account_failure"]["category"], "friend_identity_mismatch")
        self.assertEqual(chat.press.await_count, 0)
        self.assertEqual(context.close.await_count, 1)
        self.assertEqual(release.call_count, 1)


if __name__ == "__main__":
    unittest.main()
