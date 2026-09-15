import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core import protocol_dispatch, streak_state, tasks


NOW = datetime(2026, 9, 15, 11, 0, tzinfo=timezone(timedelta(hours=8)))


class StreakTargetIdentityTests(unittest.TestCase):
    def test_nickname_change_keeps_same_stable_target_ref(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "protocol_targets_cache": [
                {"nickname": "Alice", "secUid": "sec-1", "peerUserId": "1001"}
            ],
        }

        first_ref = streak_state.resolve_target_ref(account, "Alice")
        account["targets"] = ["Alice New"]
        account["protocol_targets_cache"] = [
            {"nickname": "Alice New", "secUid": "sec-1", "peerUserId": "1001"}
        ]
        second_ref = streak_state.resolve_target_ref(account, "Alice New")

        self.assertEqual("sec:sec-1", first_ref)
        self.assertEqual(first_ref, second_ref)

    def test_legacy_history_is_migrated_to_explicit_state(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("send_confirmed", state["status"])
        self.assertEqual("nickname:alice", state["targetRef"])

    def test_renamed_target_migrates_old_nickname_history(self):
        account = {
            "username": "demo",
            "targets": ["Alice New"],
            "target_refs": {"Alice": "sec:sec-1", "Alice New": "sec:sec-1"},
            "message_history": {
                "Alice": {
                    "message": "hello",
                    "sentAt": NOW.isoformat(timespec="seconds"),
                    "status": "confirmed",
                    "confirmationLevel": "strong",
                }
            },
        }

        streak_state.reconcile_account(account, NOW)
        state = streak_state.target_state(account, "Alice New", NOW)

        self.assertEqual("send_confirmed", state["status"])
        self.assertTrue(streak_state.is_send_confirmed(account, "Alice New", NOW))


class StreakStateMachineTests(unittest.TestCase):
    def test_in_flight_lease_expires_without_becoming_confirmed(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_in_flight(
            account,
            "Alice",
            run_id="run-1",
            strategy="protocol",
            now=NOW,
            lease_seconds=60,
        )

        streak_state.reconcile_account(account, NOW + timedelta(seconds=61))

        state = streak_state.target_state(account, "Alice", NOW + timedelta(seconds=61))
        self.assertEqual("failed_retryable", state["status"])
        self.assertEqual("in_flight_lease_expired", state["lastErrorCategory"])
        self.assertFalse(streak_state.is_send_confirmed(account, "Alice", NOW))

    def test_weak_and_strong_evidence_have_distinct_states(self):
        account = {"username": "demo", "targets": ["Alice"]}

        streak_state.mark_sent_unverified(
            account,
            "Alice",
            run_id="run-1",
            strategy="browser",
            now=NOW,
            detail="browser_visible_count_increased",
        )
        self.assertEqual(
            "sent_unverified",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )
        self.assertFalse(streak_state.is_send_confirmed(account, "Alice", NOW))

        streak_state.mark_send_confirmed(
            account,
            "Alice",
            run_id="run-1",
            strategy="protocol",
            now=NOW,
            detail="statusCode=0",
        )
        self.assertEqual(
            "send_confirmed",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )
        self.assertTrue(streak_state.is_send_confirmed(account, "Alice", NOW))

        streak_state.mark_streak_verified(account, "Alice", now=NOW)
        self.assertTrue(streak_state.is_streak_verified(account, "Alice", NOW))

    def test_confirmed_state_expires_on_the_next_day(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )

        next_day = NOW + timedelta(days=1)

        self.assertTrue(streak_state.is_send_confirmed(account, "Alice", NOW))
        self.assertFalse(streak_state.is_send_confirmed(account, "Alice", next_day))

    def test_failure_does_not_downgrade_confirmed_state(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_send_confirmed(
            account,
            "Alice",
            strategy="protocol",
            now=NOW,
        )

        streak_state.mark_failed(
            account,
            "Alice",
            category="browser_timeout",
            reason="late browser failure",
            retryable=True,
            now=NOW,
        )

        self.assertEqual(
            "send_confirmed",
            streak_state.target_state(account, "Alice", NOW)["status"],
        )

    def test_fallback_is_only_eligible_for_retryable_unconfirmed_failure(self):
        account = {"username": "demo", "targets": ["Alice"]}
        streak_state.mark_failed(
            account,
            "Alice",
            category="protocol_network_error",
            reason="temporary",
            retryable=True,
            now=NOW,
        )

        self.assertTrue(streak_state.fallback_eligible(account, "Alice", NOW))

        streak_state.mark_fallback_attempted(account, "Alice", now=NOW)
        self.assertFalse(streak_state.fallback_eligible(account, "Alice", NOW))
        self.assertTrue(
            streak_state.fallback_eligible(
                account,
                "Alice",
                NOW + timedelta(days=1),
            )
        )


class StreakPreflightTests(unittest.TestCase):
    def test_preflight_rejects_missing_cookies_without_consuming_target_attempts(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "cookies": [],
        }

        result = streak_state.preflight_account(account, NOW)
        state = streak_state.target_state(account, "Alice", NOW)

        self.assertFalse(result["healthy"])
        self.assertEqual("missing_cookies", result["category"])
        self.assertEqual(0, state["attemptCount"])

    def test_preflight_accepts_complete_friend_index(self):
        account = {
            "username": "demo",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
            "friend_index": {"alice": {"stableKeys": ["sec:sec-1"]}},
            "friend_index_meta": {
                "lastScanAt": NOW.isoformat(timespec="seconds"),
                "lastScanComplete": True,
            },
        }

        result = streak_state.preflight_account(
            account,
            NOW,
            require_friend_index=True,
        )

        self.assertTrue(result["healthy"])
        self.assertEqual("sec:sec-1", streak_state.resolve_target_ref(account, "Alice"))


class StreakScheduleTests(unittest.TestCase):
    def test_schedule_phases_share_one_boundary_model(self):
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 18,
            "scheduleIntervalMinutes": 20,
        }

        self.assertEqual(
            "regular",
            streak_state.schedule_phase(NOW, window),
        )
        self.assertEqual(
            "close-out",
            streak_state.schedule_phase(NOW.replace(hour=17, minute=45), window),
        )
        self.assertEqual(
            "fallback",
            streak_state.schedule_phase(NOW.replace(hour=18, minute=10), window),
        )
        self.assertEqual(
            "outside",
            streak_state.schedule_phase(NOW.replace(hour=19, minute=0), window),
        )

    def test_fallback_phase_selects_unconfirmed_targets(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 18,
            "scheduleIntervalMinutes": 20,
        }

        due, _, _, _ = tasks._select_due_targets(
            account,
            window,
            NOW.replace(hour=18, minute=10),
        )

        self.assertEqual(["Alice"], due)

    def test_end_hour_24_wraps_to_next_day_without_duplicate_fallback(self):
        window = {
            "enabled": True,
            "startHour": 10,
            "endHour": 24,
            "scheduleIntervalMinutes": 20,
        }

        self.assertEqual(
            "close-out",
            streak_state.schedule_phase(NOW.replace(hour=23, minute=50), window),
        )
        self.assertEqual(
            "fallback",
            streak_state.schedule_phase(
                NOW.replace(hour=0, minute=5) + timedelta(days=1),
                window,
            ),
        )


class StreakRunReportTests(unittest.TestCase):
    def test_run_report_is_structured_and_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.jsonl"
            streak_state.append_run_report(
                {
                    "runId": "run-1",
                    "accountRef": "account:demo",
                    "targetRef": "sec:sec-1",
                    "strategy": "protocol",
                    "attemptCount": 1,
                    "category": "",
                    "confirmationSource": "protocol_send_receipt",
                    "durationMs": 42,
                    "networkRoute": "direct",
                    "cookies": ["secret"],
                    "token": "secret",
                },
                path=path,
            )

            record = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertEqual("run-1", record["runId"])
        self.assertEqual("sec:sec-1", record["targetRef"])
        self.assertNotIn("cookies", record)
        self.assertNotIn("token", record)
        self.assertNotIn("secret", json.dumps(record, ensure_ascii=False))

    def test_report_redacts_credentials_from_free_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.jsonl"
            streak_state.append_run_report(
                {
                    "runId": "run-1",
                    "reason": (
                        "proxy failed https://user:secret@example.test "
                        "token=abc123"
                    ),
                },
                path=path,
            )
            record = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertNotIn("secret", record["reason"])
        self.assertNotIn("abc123", record["reason"])

    def test_weekly_summary_aggregates_run_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = streak_state.update_weekly_summary(
                [
                    {
                        "status": "send_confirmed",
                        "category": "",
                        "confirmationSource": "protocol_send_receipt",
                        "durationMs": 20,
                    },
                    {
                        "status": "failed_retryable",
                        "category": "protocol_network_error",
                        "confirmationSource": "",
                        "durationMs": 30,
                    },
                ],
                directory=temp_dir,
                now=NOW,
            )
            summary = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(2, summary["runs"])
        self.assertEqual(1, summary["statusCounts"]["send_confirmed"])
        self.assertEqual(1, summary["categoryCounts"]["protocol_network_error"])
        self.assertEqual(50, summary["totalDurationMs"])


class StreakTaskIntegrationTests(unittest.TestCase):
    @staticmethod
    def _update_side_effect(accounts):
        def update_accounts(mutator, force_reload=True):
            del force_reload
            value = mutator(accounts)
            if isinstance(value, tuple):
                return value[0]
            return value

        return update_accounts

    def test_protocol_fallback_claim_is_singleton(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        streak_state.mark_failed(
            account,
            "Alice",
            category="protocol_network_error",
            reason="temporary",
            retryable=True,
            now=NOW,
        )
        accounts = [account]

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            first = tasks._prepare_protocol_fallback_users(
                {"browserFallbackEnabled": True},
                [account.copy()],
            )
            second = tasks._prepare_protocol_fallback_users(
                {"browserFallbackEnabled": True},
                [account.copy()],
            )

        self.assertEqual(["Alice"], first[0]["targets"])
        self.assertEqual([], second)
        self.assertTrue(
            streak_state.target_state(account, "Alice", NOW)["fallbackAttempted"]
        )

    def test_protocol_sender_failure_marks_retryable_state(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]

        with patch.object(
            protocol_dispatch,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            protocol_dispatch._mark_protocol_targets_failed(
                account.copy(),
                "protocol_sender_failed",
                "network",
            )

        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("failed_retryable", state["status"])
        self.assertEqual("protocol_sender_failed", state["lastErrorCategory"])

    def test_scheduled_preflight_filters_unhealthy_accounts(self):
        healthy = {
            "username": "healthy",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        unhealthy = {
            "username": "unhealthy",
            "unique_id": "1002",
            "targets": ["Bob"],
            "cookies": [],
        }
        config = {"dailySendWindow": {"enabled": False}}

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(tasks, "_persist_account_preflight") as persist,
        ):
            runnable = tasks._prepare_active_users_for_run(
                config,
                [healthy, unhealthy],
            )

        self.assertEqual(["healthy"], [user["username"] for user in runnable])
        persist.assert_called_once()

    def test_protocol_exception_still_runs_selected_fallback(self):
        user = {
            "username": "demo",
            "unique_id": "1001",
            "enabled": True,
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        fallback_users = [dict(user)]
        config = {
            "multiTask": False,
            "taskCount": 1,
            "sendStrategy": {},
            "useProtocolSender": True,
            "browserSenderAccounts": [],
        }
        lock = MagicMock()
        lock.return_value.__enter__.return_value = None
        lock.return_value.__exit__.return_value = False

        with (
            patch.object(tasks, "get_config", return_value=config),
            patch.object(tasks, "get_userData", return_value=[user]),
            patch.object(tasks, "_requested_account_refs", return_value=None),
            patch.object(tasks, "_prepare_active_users_for_run", return_value=[user]),
            patch.object(tasks, "_split_sender_modes", return_value=([user], [])),
            patch.object(tasks, "task_run_lock", lock),
            patch.object(
                tasks,
                "run_protocol_tasks",
                new=AsyncMock(side_effect=RuntimeError("protocol transport failed")),
            ),
            patch.object(
                tasks,
                "_prepare_protocol_fallback_users",
                return_value=fallback_users,
            ),
            patch.object(tasks, "run_browser_tasks", new=AsyncMock()) as browser,
            patch.object(tasks, "_append_streak_run_report"),
        ):
            asyncio.run(tasks.runTasks())

        self.assertGreaterEqual(browser.await_count, 1)
        self.assertEqual(fallback_users, browser.await_args_list[0].args[1])

    def test_browser_weak_evidence_persists_unverified_state(self):
        account = {
            "username": "demo",
            "unique_id": "1001",
            "targets": ["Alice"],
            "cookies": [{"name": "sessionid", "value": "x"}],
        }
        accounts = [account]

        with patch.object(
            tasks,
            "update_user_data",
            side_effect=self._update_side_effect(accounts),
        ):
            tasks._persist_browser_send_success(
                account.copy(),
                "Alice",
                "hello",
                NOW.isoformat(timespec="seconds"),
                server_receipt=None,
            )

        history = account["message_history"]["Alice"]
        state = streak_state.target_state(account, "Alice", NOW)
        self.assertEqual("sent_unverified", history["status"])
        self.assertEqual("weak", history["confirmationLevel"])
        self.assertEqual("sent_unverified", state["status"])
        self.assertFalse(streak_state.is_send_confirmed(account, "Alice", NOW))


if __name__ == "__main__":
    unittest.main()
