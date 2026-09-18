import asyncio
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core import msg_builder, tasks
from core import send_state, streak_state
from core.send_state import history_entry_is_strong_confirmed_today
from webui import ops


class SendStateTests(unittest.TestCase):
    def test_receipt_strength_matches_the_state_machine(self):
        # A receipt whose intercepted call could not be identified is not proof
        # that a message was sent, and both modules must agree on that.
        unidentified = {"ok": True, "httpStatus": 200, "jsonOk": True}
        message_send = {
            "ok": True,
            "httpStatus": 200,
            "jsonOk": True,
            "call": "message_send",
        }

        self.assertEqual(
            streak_state.receipt_is_strong(unidentified),
            send_state._receipt_is_strong(unidentified),
        )
        self.assertFalse(send_state._receipt_is_strong(unidentified))
        self.assertTrue(send_state._receipt_is_strong(message_send))

    def setUp(self):
        self.now = datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)
        self.window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 18,
            "scheduleIntervalMinutes": 20,
        }

    def test_strong_confirmation_is_the_only_sent_state(self):
        strong = {
            "sentAt": self.now.isoformat(),
            "status": "confirmed",
            "confirmationLevel": "strong",
            "needsVerification": False,
        }
        weak = {
            "sentAt": self.now.isoformat(),
            "status": "unconfirmed",
            "confirmationLevel": "weak",
            "needsVerification": True,
        }
        legacy = {"sentAt": self.now.isoformat()}

        self.assertTrue(history_entry_is_strong_confirmed_today(strong, self.now))
        self.assertFalse(history_entry_is_strong_confirmed_today(weak, self.now))
        self.assertFalse(history_entry_is_strong_confirmed_today(legacy, self.now))

    def test_unconfirmed_target_is_visible_and_retryable(self):
        user = {
            "username": "demo",
            "unique_id": "demo",
            "targets": ["target"],
            "message_history": {
                "target": {
                    "sentAt": self.now.isoformat(),
                    "status": "unconfirmed",
                    "confirmationLevel": "weak",
                    "needsVerification": True,
                }
            },
            "failure_queue": {
                "target": {
                    "lastAttemptAt": self.now.isoformat(),
                    "category": "send_unconfirmed",
                    "attemptCount": 1,
                }
            },
        }

        status = ops._build_target_status(user, "target", self.now, self.window)

        self.assertEqual("unconfirmed", status["status"])
        self.assertFalse(tasks._target_sent_today(user, "target", self.now))
        self.assertEqual(["target"], tasks._pending_failed_targets(user, self.now))
        self.assertEqual(["target"], tasks._pending_unsent_targets(user, self.now)[0])

    def test_legacy_sent_at_only_record_is_retryable(self):
        user = {
            "targets": ["target"],
            "message_history": {"target": {"sentAt": self.now.isoformat()}},
        }

        status = ops._build_target_status(user, "target", self.now, self.window)

        self.assertEqual("unconfirmed", status["status"])
        self.assertTrue(status["legacyUnverified"])
        self.assertEqual(["target"], tasks._pending_failed_targets(user, self.now))
        self.assertEqual(["target"], tasks._pending_unsent_targets(user, self.now)[0])

    def test_unsent_retry_respects_non_retryable_and_attempt_limit(self):
        user = {
            "targets": ["blocked", "exhausted", "retryable"],
            "failure_queue": {
                "blocked": {
                    "lastAttemptAt": self.now.isoformat(),
                    "category": "protocol_user_blocked",
                    "attemptCount": 1,
                },
                "exhausted": {
                    "lastAttemptAt": self.now.isoformat(),
                    "category": "timeout",
                    "attemptCount": 3,
                },
                "retryable": {
                    "lastAttemptAt": self.now.isoformat(),
                    "category": "timeout",
                    "attemptCount": 2,
                },
            },
        }

        retryable, skipped = tasks._pending_unsent_targets(user, self.now)

        self.assertEqual(["retryable"], retryable)
        self.assertEqual(2, len(skipped))

    def test_manual_force_all_still_includes_strong_confirmed_targets(self):
        user = {
            "username": "demo",
            "targets": ["confirmed", "pending"],
            "message_history": {
                "confirmed": {
                    "sentAt": self.now.isoformat(),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                    "needsVerification": False,
                }
            },
        }
        config = {"dailySendWindow": self.window}

        with patch.dict(os.environ, {"SPARKFLOW_MANUAL_RUN": "1"}, clear=False):
            prepared = tasks._prepare_active_users_for_run(config, [user])

        self.assertEqual(["confirmed", "pending"], prepared[0]["targets"])

    def test_overlapping_task_run_is_skipped_without_traceback(self):
        config = {
            "multiTask": False,
            "taskCount": 1,
            "sendStrategy": {},
            "messageTemplate": "",
            "hitokotoTypes": [],
        }
        user = {"enabled": True, "username": "demo", "targets": ["friend"]}
        with (
            patch.object(tasks, "get_config", return_value=config),
            patch.object(tasks, "get_userData", return_value=[user]),
            patch.object(tasks, "_prepare_active_users_for_run", return_value=[user]),
            patch.object(
                tasks,
                "task_run_lock",
                side_effect=tasks.TaskRunAlreadyInProgress("already running"),
            ),
            patch.object(tasks, "run_browser_tasks") as run_browser,
        ):
            asyncio.run(tasks.runTasks())

        run_browser.assert_not_called()

    def test_message_choice_avoids_previous_and_last_when_possible(self):
        with patch.object(
            msg_builder,
            "build_message_candidates",
            return_value=["A", "B", "C"],
        ):
            selected = msg_builder.build_message(previous_message="A", last_message="B")

        self.assertEqual("C", selected)


if __name__ == "__main__":
    unittest.main()
