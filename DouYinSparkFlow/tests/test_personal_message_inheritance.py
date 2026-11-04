"""Explicitly empty personal messages inherit the configured global templates."""

from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch

from core import msg_builder
from core import personal_streak as streak
from tests import test_shared_security as fixtures
from webui import personal_streak as personal_webui
from webui import users


ALICE = {"username": "alice", "role": "user"}


class PersonalMessageInheritanceTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def save_empty(self, **changes):
        values = dict(enabled=True, at_time="10:00", end_time="17:00", timezone_name="Asia/Shanghai", messages=[])
        values.update(changes)
        return streak.save_preferences(ALICE, **values)

    def test_explicit_empty_messages_save_and_inherit_global_variants(self):
        saved = self.save_empty()
        self.assertEqual([], saved["messages"])
        groups = streak.account_groups(
            {"messageTemplate": "global fixed", "sendStrategy": {"messageVariants": ["global variant"]}},
            self.fixture.accounts,
            manual=True,
        )
        self.assertEqual(1, len(groups))
        effective, accounts = groups[0]
        self.assertEqual("global fixed", effective["messageTemplate"])
        self.assertEqual(["global variant"], effective["sendStrategy"]["messageVariants"])
        self.assertEqual("Asia/Shanghai", effective["scheduleTimezone"])
        self.assertEqual("10:00", effective["dailySendWindow"]["exactTime"])
        self.assertEqual("17:00", effective["dailySendWindow"]["endTime"])
        with msg_builder.using_message_config(effective):
            self.assertEqual(["global variant"], msg_builder.build_message_candidates())
        self.assertEqual(["a1"], [account["account_ref"] for account in accounts])

    def test_empty_personal_messages_keep_global_holiday_and_api_rules(self):
        self.save_empty()
        global_config = {
            "messageTemplate": "[API]",
            "sendStrategy": {"messageVariants": []},
            "happyNewYear": {"enabled": True, "messageTemplate": "节日 [API]"},
        }
        effective, _accounts = streak.account_groups(global_config, self.fixture.accounts, manual=True)[0]
        self.assertEqual("[API]", effective["messageTemplate"])
        self.assertEqual(global_config["happyNewYear"], effective["happyNewYear"])
        with patch.object(msg_builder, "date") as clock, patch("utils.chinese_new_year_2026_mare.get_random_festival_quote", return_value="福气"):
            clock.today.return_value = date(2026, 2, 20)
            with msg_builder.using_message_config(effective):
                self.assertEqual(["节日 福气"], msg_builder.build_message_candidates())

    def test_empty_messages_are_rejected_when_global_configuration_has_no_effective_template(self):
        empty = {"messageTemplate": "", "sendStrategy": {"messageVariants": []}, "happyNewYear": {"enabled": False, "messageTemplate": ""}}
        self.assertFalse(streak.has_global_message_templates(empty))
        with patch.object(streak, "get_config", return_value=empty), self.assertRaisesRegex(ValueError, "设置.*消息"):
            self.save_empty()

    def test_stale_empty_save_keeps_revision_conflict_precedence_over_global_validation(self):
        self.save_empty()
        empty = {"messageTemplate": "", "sendStrategy": {"messageVariants": []}, "happyNewYear": {"enabled": False, "messageTemplate": ""}}
        with patch.object(streak, "get_config", return_value=empty), self.assertRaises(streak.PreferenceConflict):
            self.save_empty(expected_revision="stale")

    def test_empty_profile_fails_closed_if_global_templates_are_later_cleared(self):
        self.save_empty()
        groups = streak.account_groups(
            {"messageTemplate": "", "sendStrategy": {"messageVariants": []}, "happyNewYear": {"enabled": False, "messageTemplate": ""}},
            self.fixture.accounts,
            manual=True,
        )
        self.assertEqual([], groups)

    def test_user_without_a_saved_profile_cannot_inherit_global_templates(self):
        self.assertEqual([], streak.account_groups({"messageTemplate": "global"}, self.fixture.accounts, manual=True))

    def test_revisionless_empty_profile_is_not_explicit_inheritance(self):
        self.save_empty()
        with users._web_users_lock():
            raw = users._load_raw()
            profile = next(iter(raw[streak.STORE_KEY].values()))
            profile.pop('revision')
            users._atomic_write_json(users.USERS_FILE, raw)
        self.assertEqual([], streak.account_groups({'messageTemplate': 'global'}, self.fixture.accounts, manual=True))

    def test_unchanged_inherited_save_preserves_active_claim_due_time_and_file_bytes(self):
        saved = self.save_empty()
        with users._web_users_lock():
            raw = users._load_raw()
            profile = next(iter(raw[streak.STORE_KEY].values()))
            profile['claim'] = {'token': 'offline-claim', 'expires': 9999999999, 'local_date': '2026-11-03'}
            users._atomic_write_json(users.USERS_FILE, raw)
        before = users.USERS_FILE.read_bytes()
        result = self.save_empty(expected_revision=saved['revision'])
        self.assertEqual(before, users.USERS_FILE.read_bytes())
        self.assertEqual('offline-claim', result['claim']['token'])
        self.assertEqual(saved['next_run_at'], result['next_run_at'])

    def test_noop_empty_save_preserves_existing_plan_and_route_overview_marks_it_configured(self):
        before = self.save_empty()
        after = self.save_empty(expected_revision=before["revision"])
        self.assertEqual(before, after)
        schedule = personal_webui.schedule_for(ALICE)
        self.assertTrue(schedule["personal"])
        self.assertNotEqual("尚未设置", schedule["label"])
        self.assertEqual("每天 10:00–17:00 · Asia/Shanghai", schedule["label"])

    def test_blank_message_post_persists_explicit_inheritance(self):
        client = self.fixture.login()
        csrf = self.fixture.token(client)
        response = client.post(
            "/preferences/streak",
            data={
                "csrf_token": csrf,
                "edit_revision": streak.preferences(ALICE)["revision"],
                "enabled": "on",
                "time": "10:00",
                "end_time": "17:00",
                "timezone": "Asia/Shanghai",
                "messages": "\n  \n",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)
        self.assertEqual([], streak.preferences(ALICE)["messages"])


if __name__ == "__main__":
    unittest.main()
