"""Protocol sender must not outlive a claimed personal end time."""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from core import personal_streak, protocol_dispatch, refusal_retry, streak_state


class ProtocolPersonalWindowTests(unittest.TestCase):
    def _claimed_account(self):
        return {
            "username": "personal",
            "unique_id": "personal",
            "targets": ["friend"],
            "_personal_scope": {"owner": "owner", "revision": "r", "claim": "claim"},
        }

    def test_claimed_personal_job_rechecks_after_queue_delay_before_claiming_or_sender(self):
        user = self._claimed_account()
        sender = Mock(return_value={"ok": True, "sent": [], "resolved": [], "unresolved": []})
        with (
            patch.object(personal_streak, "authorized", return_value=True),
            patch.object(personal_streak, "within_send_window", side_effect=[True, False]) as window,
            patch.object(protocol_dispatch.random, "randint", return_value=1),
            patch.object(protocol_dispatch.asyncio, "sleep", new=AsyncMock()),
            patch.object(protocol_dispatch, "_mark_protocol_targets_in_flight") as claim,
            patch.object(protocol_dispatch, "build_messages_for_targets") as messages,
            patch.object(protocol_dispatch, "_run_protocol_for_user", side_effect=sender),
            patch.object(protocol_dispatch, "_merge_protocol_runtime_state"),
        ):
            result = asyncio.run(protocol_dispatch.run_protocol_tasks(
                {"multiTask": False, "sendStrategy": {"accountStartDelaySecondsMin": 1, "accountStartDelaySecondsMax": 1}},
                [user], None, run_id="personal-run",
            ))

        self.assertEqual(result[0]["runner"], "personal-window-closed")
        self.assertEqual(window.call_count, 2)
        claim.assert_not_called()
        messages.assert_not_called()
        sender.assert_not_called()

    def test_claimed_personal_deadline_is_passed_to_protocol_child(self):
        import json
        from types import SimpleNamespace
        user = self._claimed_account()
        captured = {}
        def fake_run(command, **kwargs):
            captured.update(json.loads(kwargs["input"]))
            return SimpleNamespace(returncode=0, stdout='{"ok": true, "sent": [], "resolved": [], "unresolved": []}', stderr="")
        with (
            patch.object(protocol_dispatch, "_build_protocol_command", return_value=(['node', 'offline'], '.', 'test', '.', None)),
            patch.object(personal_streak, "protocol_send_deadline", return_value=123456),
            patch.object(protocol_dispatch.subprocess, "run", side_effect=fake_run),
        ):
            result = protocol_dispatch._run_protocol_for_user(user, {"friend": "hello"}, False, {})

        self.assertTrue(result["ok"])
        self.assertEqual(captured["personalSendDeadlineEpochMs"], 123456)

    def test_manual_personal_scope_keeps_protocol_no_claim_window_bypass(self):
        user = self._claimed_account()
        user["_personal_scope"]["claim"] = ""
        sender_result = {"ok": True, "sent": [], "resolved": [], "unresolved": []}
        with (
            patch.object(personal_streak, "authorized", return_value=True),
            patch.object(personal_streak, "within_send_window") as window,
            patch.object(protocol_dispatch, "_mark_protocol_targets_in_flight", return_value=["friend"]),
            patch.object(protocol_dispatch, "build_messages_for_targets", return_value={"friend": "hello"}),
            patch.object(protocol_dispatch, "_run_protocol_for_user", return_value=sender_result) as sender,
            patch.object(protocol_dispatch, "_merge_protocol_runtime_state"),
        ):
            result = asyncio.run(protocol_dispatch.run_protocol_tasks(
                {"multiTask": False}, [user], None, run_id="manual-run",
            ))

        self.assertEqual(result, [sender_result])
        window.assert_not_called()
        sender.assert_called_once()


class ProtocolPersonalQuotaDurabilityTests(unittest.TestCase):
    """Protocol quota checks must retain authorized personal day provenance."""

    def setUp(self):
        from utils import config
        from webui import users

        self.config = config
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        accounts_path = root / "accounts.json"
        self._patches = [
            patch.object(config, "users_data_path", return_value=accounts_path),
            patch.object(config, "get_environment", return_value=config.Environment.LOCAL),
            patch.object(users, "USERS_FILE", root / "users.json"),
            patch.dict("os.environ", {"SPARKFLOW_STORAGE_REQUIRE_ENCRYPTED": "0"}),
        ]
        for context in self._patches:
            context.start()
            self.addCleanup(context.stop)
        previous_cache = config.userData
        config.userData = None
        self.addCleanup(setattr, config, "userData", previous_cache)
        config._save_json_file(accounts_path, [{
            "account_ref": "a1", "unique_id": "111", "username": "Alice account",
            "targets": ["friend"], "enabled": True,
        }])
        users._atomic_write_json(users.USERS_FILE, {
            "auth_epoch": 0,
            "users": [{
                "username": "alice", "user_id": "alice-id", "role": "user",
                "enabled": True, "account_refs": ["a1"],
            }],
        })
        personal_streak.save_preferences(
            {"username": "alice", "role": "user"},
            enabled=True,
            at_time="00:30",
            timezone_name="Asia/Tokyo",
            messages=["hello"],
        )
        self.user = personal_streak.account_groups(
            {}, config.get_userData(force_reload=True), manual=True,
        )[0][1][0]
        self.tokyo = ZoneInfo("Asia/Tokyo")
        self.shanghai = ZoneInfo("Asia/Shanghai")
        self.now = datetime(2026, 11, 3, 15, 30, tzinfo=timezone.utc)
        self.env = patch.dict("os.environ", {"SPARKFLOW_TIMEZONE": "Asia/Shanghai"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _mark(self, user, run_id, *, allow_retry=False, readback=None):
        readback = readback or (lambda **kwargs: self.config.get_userData(force_reload=True))
        with (
            patch.object(protocol_dispatch, "datetime") as clock,
            patch.object(
                protocol_dispatch, "update_user_data",
                side_effect=lambda mutate, **kwargs: self.config.update_user_data(mutate),
            ),
            patch.object(protocol_dispatch, "get_userData", side_effect=readback),
        ):
            clock.now.return_value = self.now
            return protocol_dispatch._mark_protocol_targets_in_flight(
                user, run_id, allow_retry=allow_retry,
            )

    def _write(self, mutate):
        self.config.update_user_data(mutate)

    def test_authorized_tokyo_policy_claims_after_global_day_rejection(self):
        def reject_on_prior_tokyo_day(accounts):
            streak_state.mark_failed(
                accounts[0], "friend", category="send_rejected", reason="8610",
                retryable=False,
                now=datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc),
            )
            return None, True

        self._write(reject_on_prior_tokyo_day)

        self.assertEqual(self._mark(self.user, "tokyo-new-day"), ["friend"])
        reloaded = self.config.get_userData(force_reload=True)[0]
        self.assertNotIn("_personal_scope", reloaded)
        self.assertTrue(refusal_retry.rejection_precedes_today(
            reloaded, "friend", self.now, schedule_timezone=self.tokyo,
        ))
        self.assertFalse(refusal_retry.rejection_precedes_today(
            reloaded, "friend", self.now, schedule_timezone=self.shanghai,
        ))

    def test_current_day_manual_refusal_is_reserved_once_after_disk_readback(self):
        def rejected_today(accounts):
            streak_state.mark_failed(
                accounts[0], "friend", category="send_rejected", reason="8610",
                retryable=False, now=self.now - timedelta(minutes=21),
            )
            return None, True

        self._write(rejected_today)

        self.assertEqual(self._mark(self.user, "manual-once", allow_retry=True), ["friend"])
        self.assertEqual(self._mark(self.user, "manual-twice", allow_retry=True), [])
        reloaded = self.config.get_userData(force_reload=True)[0]
        self.assertTrue(refusal_retry.used_today(
            reloaded, "friend", self.now, schedule_timezone=self.tokyo,
        ))

    def test_inconsistent_reservation_readback_returns_no_claim(self):
        def rejected_today(accounts):
            streak_state.mark_failed(
                accounts[0], "friend", category="send_rejected", reason="8610",
                retryable=False, now=self.now - timedelta(minutes=21),
            )
            return None, True

        self._write(rejected_today)

        def erase_reservation(**kwargs):
            accounts = self.config.get_userData(force_reload=True)
            accounts[0].pop(refusal_retry.KEY, None)
            self.config._save_json_file(self.config.users_data_path(), accounts)
            return self.config.get_userData(force_reload=True)

        self.assertEqual(
            self._mark(self.user, "lost-readback", allow_retry=True, readback=erase_reservation),
            [],
        )

    def test_spoofed_personal_timezone_fails_closed(self):
        forged = dict(self.user)
        forged["_personal_scope"] = dict(
            self.user["_personal_scope"], schedule_timezone="America/New_York",
        )

        self.assertEqual(self._mark(forged, "forged-timezone"), [])
        self.assertNotIn("target_states", self.config.get_userData(force_reload=True)[0])

    def test_stale_personal_scope_fails_closed(self):
        personal_streak.save_preferences(
            {"username": "alice", "role": "user"},
            enabled=True,
            at_time="00:31",
            timezone_name="Asia/Tokyo",
            messages=["changed"],
        )

        self.assertEqual(self._mark(self.user, "stale-scope"), [])
        self.assertNotIn("target_states", self.config.get_userData(force_reload=True)[0])
