import asyncio
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch
from contextlib import nullcontext

from core import personal_streak as streak
from core.msg_builder import build_message_candidates, using_message_config
from core.personal_dispatcher import PersonalDispatcher
from tests import test_shared_security as fixtures
from utils import config
from webui import app as app_module, users


NOW = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
ALICE = {"username": "alice", "role": "user"}
BOB = {"username": "bob", "role": "user"}


class PersonalStreakTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.clock = patch.object(streak, "_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def save(self, principal=ALICE, **changes):
        values = dict(enabled=True, at_time="08:30", timezone_name="Asia/Shanghai", messages=["早安，今天也加油"])
        values.update(changes)
        return streak.save_preferences(principal, **values)

    def due(self, principal=ALICE, **changes):
        saved = self.save(principal, **changes)
        return streak.claim_due(datetime.fromisoformat(saved["next_run_at"]))

    def test_two_users_keep_separate_time_content_and_encrypted_storage(self):
        self.save()
        self.save(BOB, at_time="21:15", messages=["晚上好"])
        self.assertEqual("08:30", streak.preferences(ALICE)["time"])
        self.assertEqual(["晚上好"], streak.preferences(BOB)["messages"])
        stored = users.USERS_FILE.read_text()
        self.assertNotIn("晚上好", stored)
        self.assertNotIn("早安", stored)
        self.assertEqual(0o600, users.USERS_FILE.stat().st_mode & 0o777)

    def test_preferences_survive_user_store_updates(self):
        saved = self.save()
        users.save_web_users(users.get_web_users())
        self.assertEqual(saved["messages"], streak.preferences(ALICE)["messages"])

    def test_global_scheduled_and_fallback_jobs_exclude_personal_accounts(self):
        self.save()
        self.assertEqual([], streak.account_groups({}, self.fixture.accounts, manual=False))

    def test_manual_sending_uses_own_literal_messages_without_global_holiday_override(self):
        self.save(messages=["[API] 这是我的文字"])
        self.save(BOB, at_time="21:15", messages=["晚安"])
        groups = streak.account_groups({"messageTemplate": "global", "happyNewYear": {"enabled": True}}, self.fixture.accounts, manual=True)
        self.assertEqual(2, len(groups))
        for effective, accounts in groups:
            with using_message_config(effective):
                self.assertEqual([effective["messageTemplate"]], build_message_candidates())
            self.assertEqual(1, len(accounts))
        self.assertEqual("08:30", groups[0][0]["dailySendWindow"]["exactTime"])
        self.assertEqual("21:15", groups[1][0]["dailySendWindow"]["exactTime"])

    def test_unconfigured_user_cannot_send_global_content(self):
        self.assertEqual([], streak.account_groups({"messageTemplate": "global"}, self.fixture.accounts, manual=True))

    def test_claim_is_exclusive_durable_and_finishes_on_next_local_day(self):
        job = self.due()
        due = NOW + timedelta(minutes=45)
        self.assertIsNone(streak.claim_due(due))
        streak.finish_claim(job, now=due)
        self.assertEqual("2026-11-03T00:30:00+00:00", streak.preferences(ALICE)["next_run_at"])
        self.assertIsNone(streak.claim_due(due))

    def test_busy_job_retries_in_one_minute(self):
        job = self.due()
        streak.finish_claim(job, busy=True, now=NOW)
        self.assertEqual((NOW + timedelta(minutes=1)).isoformat(), streak.preferences(ALICE)["next_run_at"])

    def test_expired_claim_can_be_recovered_but_old_job_cannot_finish_new_claim(self):
        old = self.due()
        new = streak.claim_due(NOW + timedelta(hours=2))
        self.assertNotEqual(old["token"], new["token"])
        streak.finish_claim(old, now=NOW)
        key = new["key"]
        self.assertEqual(new["token"], users._load_raw()[streak.STORE_KEY][key]["claim"]["token"])

    def test_disabled_user_and_changed_ownership_revoke_in_flight_scope(self):
        self.save()
        account = streak.account_groups({}, self.fixture.accounts, manual=True)[0][1][0]
        self.assertTrue(streak.authorized(account))
        people = users.get_web_users()
        people[0]["enabled"] = False
        users.save_web_users(people)
        self.assertFalse(streak.authorized(account))
        people[0]["enabled"] = True
        people[0]["account_refs"] = []
        people[1]["account_refs"].append("a1")
        users.save_web_users(people)
        self.assertFalse(streak.authorized(account))

    def test_editing_preferences_invalidates_old_scope_and_claim(self):
        job = self.due()
        account = streak.account_groups({}, self.fixture.accounts, profile_key=job["key"], revision=job["revision"], claim=job["token"])[0][1][0]
        self.assertTrue(streak.authorized(account))
        self.save(messages=["新的内容"])
        self.assertFalse(streak.authorized(account))
        self.assertEqual([], streak.account_groups({}, self.fixture.accounts, profile_key=job["key"], revision=job["revision"], claim=job["token"]))

    def test_paused_deleted_or_relogged_account_cannot_continue_old_run(self):
        self.save()
        account = streak.account_groups({}, self.fixture.accounts, manual=True)[0][1][0]
        original = config.get_userData(force_reload=True)
        for changed in ([dict(original[0], enabled=False), original[1]], [original[1]],
                        [dict(original[0], login_generation="new-login", cookies=[{"name": "sessionid", "value": "new-session"}]), original[1]]):
            config.save_userData(changed)
            self.assertFalse(streak.authorized(account))
        config.save_userData(original)
        self.assertTrue(streak.authorized(account))

    def test_saving_same_form_does_not_reset_next_run_or_revision(self):
        before = self.save()
        self.clock.return_value = NOW + timedelta(minutes=10)
        self.assertEqual(before, self.save())

    def test_invalid_time_zone_and_message_inputs_are_rejected(self):
        for changes in ({"at_time": "24:00"}, {"timezone_name": "../secret"}, {"messages": []}, {"messages": ["x" * 501]}, {"messages": ["a\0b"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.save(**changes)

    def test_account_required_and_disabled_principal_cannot_save(self):
        with self.assertRaises(ValueError):
            self.save({"username": "owner", "role": "admin"})
        people = users.get_web_users()
        people[0]["enabled"] = False
        users.save_web_users(people)
        with self.assertRaises(ValueError):
            self.save()

    def test_form_cannot_select_other_user_or_bypass_csrf(self):
        alice = self.fixture.login()
        csrf = self.fixture.token(alice)
        form = {"csrf_token": csrf, "enabled": "on", "time": "09:15", "timezone": "Asia/Shanghai", "messages": "自己的文字", "user_id": users.get_web_users()[1]["user_id"], "username": "bob"}
        response = alice.post("/preferences/streak", data=form, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual(["自己的文字"], streak.preferences(ALICE)["messages"])
        self.assertEqual([], streak.preferences(BOB)["messages"])
        self.assertEqual(403, alice.post("/preferences/streak", data={**form, "csrf_token": "wrong"}).status_code)
        self.assertNotIn("自己的文字", self.fixture.login("bob", "Second-secret-2026!").get("/preferences/streak").text)

    def test_only_claimed_user_accounts_are_selected_for_personal_run(self):
        job = self.due()
        self.save(BOB)
        groups = streak.account_groups({}, self.fixture.accounts, profile_key=job["key"], revision=job["revision"], claim=job["token"])
        self.assertEqual(["a1"], [a["account_ref"] for _, accounts in groups for a in accounts])

    def test_dispatcher_waits_for_child_and_retries_busy_jobs(self):
        job = self.due()
        child = Mock()
        child.poll.side_effect = [None, 75]
        with patch("core.personal_dispatcher.claim_due", side_effect=[job, None]), patch("core.personal_dispatcher.subprocess.Popen", return_value=child) as spawn, patch("core.personal_dispatcher.finish_claim") as finish, patch("core.personal_dispatcher.renew_claim") as renew, patch.dict(os.environ, {"SPARKFLOW_MANUAL_FORCE_ALL": "1", "SPARKFLOW_TASK_MODE": "patrol"}):
            dispatcher = PersonalDispatcher()
            dispatcher.tick()
            dispatcher.tick()
            renew.assert_called_once_with(job)
            dispatcher.tick()
            finish.assert_called_once_with(job, busy=True)
            env = spawn.call_args.kwargs["env"]
            self.assertNotIn("SPARKFLOW_MANUAL_FORCE_ALL", env)
            self.assertNotIn("SPARKFLOW_TASK_MODE", env)
            self.assertEqual(job["token"], env["SPARKFLOW_PERSONAL_CLAIM"])

    def test_actual_task_entry_uses_own_content_and_scoped_account(self):
        from core import tasks
        self.save(messages=["Alice only"])
        self.save(BOB, messages=["Bob only"])
        configuration = {"multiTask": False, "taskCount": 1, "sendStrategy": {}, "useProtocolSender": False}
        with patch.object(tasks, "get_config", return_value=configuration), patch.object(tasks, "get_userData", return_value=self.fixture.accounts), patch.object(tasks, "_requested_account_refs", return_value={"a1"}), patch.object(tasks, "_prepare_active_users_for_run", side_effect=lambda cfg, accounts: accounts), patch.object(tasks, "task_run_lock", return_value=nullcontext()), patch.object(tasks, "_append_streak_run_report"), patch.object(tasks.notifications, "notify_run_finished"), patch.object(tasks, "run_browser_tasks", new_callable=AsyncMock) as browser, patch.dict(os.environ, {"SPARKFLOW_MANUAL_RUN": "1", "SPARKFLOW_FALLBACK_PHASE": "0"}):
            self.assertEqual("completed", asyncio.run(tasks.runTasks()))
            browser.assert_awaited_once()
            effective, accounts = browser.await_args.args
            self.assertEqual("Alice only", effective["messageTemplate"])
            self.assertEqual(["a1"], [account["account_ref"] for account in accounts])

    def test_protocol_fallback_keeps_original_authorization_after_reloading_storage(self):
        from core import tasks
        self.save()
        account = streak.account_groups({}, self.fixture.accounts, manual=True)[0][1][0]
        account["targets"] = ["friend"]
        with patch.object(tasks.streak_state, "fallback_eligible", return_value=True):
            fallback = tasks._prepare_protocol_fallback_users({}, [account])
        self.assertEqual(account["_personal_scope"], fallback[0]["_personal_scope"])
        self.save(messages=["changed"])
        self.assertFalse(streak.authorized(fallback[0]))


class PersonalTimeTests(unittest.TestCase):
    def test_dst_gap_and_repeat_produce_one_future_daily_time(self):
        profile = {"time": "02:30", "timezone": "America/New_York"}
        self.assertEqual(datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc), streak.next_run(profile, datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)))
        profile["time"] = "01:30"
        self.assertEqual(datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc), streak.next_run(profile, datetime(2026, 11, 1, 5, 45, tzinfo=timezone.utc)))

    def test_message_context_is_separate_for_concurrent_jobs(self):
        async def render(text):
            with using_message_config({"messageTemplate": text}):
                await asyncio.sleep(0)
                return build_message_candidates()
        async def scenario():
            return await asyncio.gather(render("Alice"), render("Bob"))
        self.assertEqual([["Alice"], ["Bob"]], asyncio.run(scenario()))
