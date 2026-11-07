"""Offline console distinctions for pending, blocked and unknown sends."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import streak_state
from webui import ops

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
WINDOW = {"enabled": False}


class PipelineConsoleTests(unittest.TestCase):
    def account(self):
        return {"username": "demo-pipeline", "unique_id": "demo-pipeline", "targets": ["Alice", "Bob", "Carol"]}

    def test_unattempted_blocked_target_does_not_inherit_account_attempt(self):
        account = self.account()
        item = ops._build_target_status(account, "Carol", NOW, WINDOW)
        failure = {"category": "browser_stage_timeout", "reason": "browser_stage_timeout stage=list_rows",
                   "lastAttemptAt": NOW.isoformat(), "attemptCount": 2, "affectedTargets": ["Bob", "Carol"]}
        blocked = ops._account_blocked_target_status(account, item, failure, NOW)
        self.assertEqual(blocked["status"], "account_blocked")
        self.assertEqual(blocked["lastAttemptAt"], "")
        self.assertEqual(blocked["attemptCount"], 0)
        self.assertEqual(blocked["accountLastAttemptAt"], NOW.isoformat())
        self.assertEqual(blocked["accountAttemptCount"], 2)
        self.assertIn("未执行", blocked["skipReason"])

    def test_barrier_without_history_is_unknown_not_pending(self):
        account = self.account()
        streak_state.mark_sent_unverified(account, "Bob", strategy="browser", run_id="offline",
                                          now=NOW, detail="pre_enter_outcome_unknown_barrier")
        item = ops._build_target_status(account, "Bob", NOW, WINDOW)
        self.assertEqual(item["status"], "unconfirmed")
        self.assertTrue(item["needsVerification"])
        self.assertEqual(item["confirmationSource"], "pre_enter_outcome_unknown_barrier")
        self.assertIn("不自动重发", item["skipReason"])
        self.assertIn("不代表已经发出", item["reason"])
        self.assertIn("不代表页面有回显", item["reason"])

    def test_strong_history_precedes_a_conservative_unknown_barrier(self):
        account = self.account()
        streak_state.mark_sent_unverified(account, "Bob", strategy="browser", run_id="offline",
                                          now=NOW, detail="pre_enter_outcome_unknown_barrier")
        account["message_history"] = {"Bob": {
            "sentAt": NOW.isoformat(), "status": "confirmed", "confirmationLevel": "strong",
            "confirmationSource": "browser_server_receipt", "message": "synthetic greeting",
        }}
        item = ops._build_target_status(account, "Bob", NOW, WINDOW)
        self.assertEqual(item["status"], "sent")
        self.assertFalse(item["needsVerification"])

    def test_previous_day_barrier_does_not_become_todays_unknown(self):
        account = self.account()
        streak_state.mark_sent_unverified(account, "Bob", strategy="browser", run_id="offline",
                                          now=NOW-timedelta(days=1), detail="pre_enter_outcome_unknown_barrier")
        item = ops._build_target_status(account, "Bob", NOW, WINDOW)
        self.assertEqual(item["status"], "unprocessed")

    def test_login_block_does_not_promise_automatic_recovery_tomorrow(self):
        account = self.account()
        item = ops._build_target_status(account, "Carol", NOW, WINDOW)
        failure = {"category": "login_required", "lastAttemptAt": NOW.isoformat(), "attemptCount": 1}
        blocked = ops._account_blocked_target_status(account, item, failure, NOW)
        self.assertNotIn("明天恢复", blocked["skipReason"])
        self.assertIn("重新登录", blocked["skipReason"])


if __name__ == "__main__":
    unittest.main()
