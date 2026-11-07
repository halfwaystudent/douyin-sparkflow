"""Protocol claim compatibility and authorization guards for legacy scopes."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import unittest
from unittest.mock import patch

from core import personal_streak, protocol_dispatch, streak_state
from tests import test_shared_security as fixtures
from utils import config
from webui import users


NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)


class ProtocolLegacyClaimTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.account = dict(fixture.accounts[0], enabled=True, targets=["friend"])
        config.save_userData([self.account, fixture.accounts[1]])
        self.alice_key = users.stable_user_id(users.find_web_user("alice"))

    def set_legacy_mapping(self, mapping):
        raw = users._load_raw()
        raw["legacy_global_schedule"] = mapping
        raw.pop(personal_streak.STORE_KEY, None)
        users._atomic_write_json(users.USERS_FILE, raw)

    def legacy_account(self):
        self.set_legacy_mapping({self.alice_key: {"account_refs": ["a1"]}})
        groups = personal_streak.account_groups({}, config.get_userData(force_reload=True), manual=False)
        self.assertEqual(1, len(groups))
        account = groups[0][1][0]
        self.assertTrue(account["_personal_scope"]["legacy_global_schedule"])
        self.assertTrue(personal_streak.authorized(account))
        self.assertIsNone(personal_streak.authorized_schedule_timezone(account))
        return account

    def personal_account(self, timezone_name="Asia/Tokyo"):
        personal_streak.save_preferences(
            {"username": "alice", "role": "user"},
            enabled=True,
            at_time="08:30",
            timezone_name=timezone_name,
            messages=["personal"],
        )
        groups = personal_streak.account_groups({}, config.get_userData(force_reload=True), manual=True)
        self.assertEqual(1, len(groups))
        account = groups[0][1][0]
        self.assertFalse(account["_personal_scope"]["legacy_global_schedule"])
        self.assertTrue(personal_streak.authorized(account))
        return account

    def test_authorized_legacy_scope_claims_and_persists_with_global_policy(self):
        account = self.legacy_account()

        with patch.object(protocol_dispatch, "datetime") as clock, \
             patch.object(protocol_dispatch.refusal_retry, "used_today", wraps=protocol_dispatch.refusal_retry.used_today) as used_today:
            clock.now.return_value = NOW
            claimed = protocol_dispatch._mark_protocol_targets_in_flight(account, "legacy-run")

        self.assertEqual(["friend"], claimed)
        self.assertIsNone(used_today.call_args.kwargs["schedule_timezone"])
        persisted = config.get_userData(force_reload=True)[0]
        state = streak_state.target_state(persisted, "friend", NOW)
        self.assertEqual("in_flight", state["status"])
        self.assertEqual("legacy-run", state["runId"])

    def test_revoked_legacy_scope_is_rejected_without_persisting(self):
        account = self.legacy_account()
        self.set_legacy_mapping({})
        self.assertFalse(personal_streak.authorized(account))

        with patch.object(protocol_dispatch, "datetime") as clock, \
             patch.object(protocol_dispatch, "update_user_data") as persist:
            clock.now.return_value = NOW
            self.assertEqual(
                [], protocol_dispatch._mark_protocol_targets_in_flight(account, "revoked-legacy")
            )

        persist.assert_not_called()

    def test_manual_personal_scope_claims_with_its_validated_timezone(self):
        account = self.personal_account()

        with patch.object(protocol_dispatch, "datetime") as clock, \
             patch.object(protocol_dispatch.refusal_retry, "used_today", wraps=protocol_dispatch.refusal_retry.used_today) as used_today:
            clock.now.return_value = NOW
            claimed = protocol_dispatch._mark_protocol_targets_in_flight(account, "personal-run")

        self.assertEqual(["friend"], claimed)
        self.assertEqual(ZoneInfo("Asia/Tokyo"), used_today.call_args.kwargs["schedule_timezone"])
        state = streak_state.target_state(config.get_userData(force_reload=True)[0], "friend", NOW)
        self.assertEqual("personal-run", state["runId"])

    def test_forged_legacy_marker_cannot_bypass_personal_authorization(self):
        account = self.personal_account()
        forged = dict(account)
        forged["_personal_scope"] = dict(account["_personal_scope"], legacy_global_schedule=True)
        self.assertFalse(personal_streak.authorized(forged))

        with patch.object(protocol_dispatch, "datetime") as clock, \
             patch.object(protocol_dispatch, "update_user_data") as persist:
            clock.now.return_value = NOW
            self.assertEqual(
                [], protocol_dispatch._mark_protocol_targets_in_flight(forged, "forged-legacy")
            )

        persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
