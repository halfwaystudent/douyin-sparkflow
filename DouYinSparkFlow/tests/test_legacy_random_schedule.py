import unittest

from core import personal_streak as streak
from tests import test_shared_security as fixtures
from utils import config
from webui import users


class LegacyRandomScheduleTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        users.create_web_user("carol", "Third-secret-2026!", account_refs=[])
        users.create_web_user("dave", "Fourth-secret-2026!", account_refs=[])
        self.accounts = [dict(fixture.accounts[0], enabled=True), dict(fixture.accounts[1], enabled=False)]
        config.save_userData(self.accounts)
        self.alice_key = users.stable_user_id(users.find_web_user("alice"))

    def set_legacy_mapping(self, mapping):
        raw = users._load_raw()
        raw["legacy_global_schedule"] = mapping
        raw.pop(streak.STORE_KEY, None)
        users._atomic_write_json(users.USERS_FILE, raw)

    def test_explicit_mapped_existing_owner_keeps_original_global_schedule(self):
        original = {
            "messageTemplate": "original random copy",
            "dailySendWindow": {"enabled": True, "startHour": 10, "endHour": 17, "scheduleIntervalMinutes": 30},
            "sendStrategy": {"messageVariants": ["original random copy", "another original copy"]},
        }
        self.set_legacy_mapping({self.alice_key: {"account_refs": ["a1"]}})

        groups = streak.account_groups(original, self.accounts, manual=False)

        self.assertEqual(1, len(groups))
        effective, accounts = groups[0]
        self.assertEqual(original, effective)
        self.assertEqual(["a1"], [account["account_ref"] for account in accounts])

    def test_legacy_scope_is_revoked_when_mapping_is_removed(self):
        self.set_legacy_mapping({self.alice_key: {"account_refs": ["a1"]}})
        account = streak.account_groups({}, self.accounts, manual=False)[0][1][0]
        self.assertTrue(streak.authorized(account))

        self.set_legacy_mapping({})

        self.assertFalse(streak.authorized(account))

    def test_profile_presence_overrides_legacy_and_unmapped_users_are_not_scheduled(self):
        self.set_legacy_mapping({self.alice_key: {"account_refs": ["a1"]}})
        raw = users._load_raw()
        raw[streak.STORE_KEY] = {self.alice_key: {"enabled": False}}
        users._atomic_write_json(users.USERS_FILE, raw)

        self.assertEqual([], streak.account_groups({"messageTemplate": "global"}, self.accounts, manual=False))

        self.set_legacy_mapping({})
        self.assertEqual([], streak.account_groups({"messageTemplate": "global"}, self.accounts, manual=False))

    def test_existing_legacy_scope_is_revoked_when_a_disabled_profile_appears(self):
        self.set_legacy_mapping({self.alice_key: {"account_refs": ["a1"]}})
        account = streak.account_groups({}, self.accounts, manual=False)[0][1][0]
        raw = users._load_raw()
        raw[streak.STORE_KEY] = {self.alice_key: {"enabled": False}}
        users._atomic_write_json(users.USERS_FILE, raw)

        self.assertFalse(streak.authorized(account))

    def test_legacy_scope_is_revoked_by_disabled_owner_reassignment_or_relogin(self):
        self.set_legacy_mapping({self.alice_key: {"account_refs": ["a1"]}})
        account = streak.account_groups({}, self.accounts, manual=False)[0][1][0]
        people = users.get_web_users()
        people[0]["enabled"] = False
        users.save_web_users(people)
        self.assertFalse(streak.authorized(account))

        people = users.get_web_users()
        people[0]["enabled"] = True
        people[0]["account_refs"] = []
        people[1]["account_refs"].append("a1")
        users.save_web_users(people)
        self.assertFalse(streak.authorized(account))

        people = users.get_web_users()
        people[0]["account_refs"] = ["a1"]
        people[1]["account_refs"].remove("a1")
        users.save_web_users(people)
        fresh = streak.account_groups({}, self.accounts, manual=False)[0][1][0]
        config.save_userData([dict(self.accounts[0], login_generation="new-login"), self.accounts[1]])
        self.assertFalse(streak.authorized(fresh))

    def test_explicit_legacy_mapping_preserves_manual_retry_configuration(self):
        original = {"messageTemplate": "legacy", "dailySendWindow": {"enabled": True, "startHour": 10, "endHour": 17}}
        self.set_legacy_mapping({self.alice_key: {"account_refs": ["a1"]}})
        groups = streak.account_groups(original, self.accounts, manual=True)
        self.assertEqual(1, len(groups))
        self.assertEqual(original, groups[0][0])
        self.assertEqual(["a1"], [a["account_ref"] for a in groups[0][1]])
        self.assertTrue(streak.authorized(groups[0][1][0]))

    def test_legacy_mapping_never_selects_a_disabled_account(self):
        self.set_legacy_mapping({self.alice_key: {"account_refs": ["a1"]}})
        disabled = [dict(self.accounts[0], enabled=False), self.accounts[1]]

        self.assertEqual([], streak.account_groups({}, disabled, manual=False))


if __name__ == "__main__":
    unittest.main()
