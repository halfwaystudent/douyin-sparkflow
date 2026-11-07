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
        self.assertEqual("24:00", streak.preferences(ALICE)["end_time"])
        self.assertEqual(["晚上好"], streak.preferences(BOB)["messages"])
        stored = users.USERS_FILE.read_text()
        self.assertNotIn("晚上好", stored)
        self.assertNotIn("早安", stored)
        self.assertEqual(0o600, users.USERS_FILE.stat().st_mode & 0o777)

    def test_end_time_round_trips_and_scopes_the_personal_window(self):
        saved = self.save(end_time="09:17")

        self.assertEqual("09:17", saved["end_time"])
        self.assertEqual("09:17", streak.preferences(ALICE)["end_time"])
        effective, _accounts = streak.account_groups({}, self.fixture.accounts, manual=True)[0]
        self.assertEqual("08:30", effective["dailySendWindow"]["exactTime"])
        self.assertEqual("09:17", effective["dailySendWindow"]["endTime"])

    def test_legacy_save_without_end_time_preserves_existing_end_time(self):
        self.save(end_time="09:17")
        self.save(messages=["更新后的消息"])

        self.assertEqual("09:17", streak.preferences(ALICE)["end_time"])

    def test_end_time_must_be_later_than_start_on_the_same_day(self):
        for end_time in ("08:30", "08:29", "24:01", "not-a-time"):
            with self.subTest(end_time=end_time), self.assertRaisesRegex(ValueError, "结束时间"):
                self.save(end_time=end_time)

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

    def test_claim_and_busy_retry_do_not_cross_personal_end_time(self):
        saved = self.save(end_time="08:31")
        due = datetime.fromisoformat(saved["next_run_at"])
        self.assertIsNone(streak.claim_due(due + timedelta(minutes=2)))
        self.assertEqual("2026-11-03T00:30:00+00:00", streak.preferences(ALICE)["next_run_at"])

        job = self.due(end_time="08:31")
        streak.finish_claim(job, busy=True, now=due + timedelta(seconds=30))
        self.assertEqual("2026-11-03T00:30:00+00:00", streak.preferences(ALICE)["next_run_at"])

    def test_start_failure_after_end_boundary_does_not_claim_a_retry(self):
        job = self.due(end_time="08:31")
        streak.finish_claim(job, outcome="failed", now=NOW + timedelta(minutes=30, seconds=30))

        profile = streak.preferences(ALICE)
        self.assertFalse(profile["last_run"]["retry_scheduled"])
        self.assertEqual("2026-11-03T00:30:00+00:00", profile["next_run_at"])

    def test_claimed_personal_protocol_deadline_uses_saved_profile_timezone_and_end_time(self):
        job = self.due(end_time="08:31")
        account = streak.account_groups(
            {}, self.fixture.accounts, profile_key=job["key"],
            revision=job["revision"], claim=job["token"],
        )[0][1][0]

        deadline = streak.protocol_send_deadline(account, now=NOW + timedelta(minutes=30))

        self.assertEqual(
            datetime.fromtimestamp(deadline / 1000, timezone.utc),
            NOW + timedelta(minutes=31),
        )

    def test_claim_expiry_caps_protocol_deadline_and_expired_claim_is_rejected(self):
        job = self.due(end_time="08:31")
        with users._web_users_lock():
            stored = users._load_raw()
            stored[streak.STORE_KEY][job["key"]]["claim"]["expires"] = (NOW + timedelta(minutes=20)).timestamp()
            users._atomic_write_json(users.USERS_FILE, stored)
        account = streak.account_groups(
            {}, self.fixture.accounts, profile_key=job["key"],
            revision=job["revision"], claim=job["token"],
        )[0][1][0]

        deadline = streak.protocol_send_deadline(account, now=NOW)
        self.assertEqual(
            datetime.fromtimestamp(deadline / 1000, timezone.utc),
            NOW + timedelta(minutes=20),
        )

        with users._web_users_lock():
            stored = users._load_raw()
            stored[streak.STORE_KEY][job["key"]]["claim"]["expires"] = (NOW - timedelta(seconds=1)).timestamp()
            users._atomic_write_json(users.USERS_FILE, stored)
        self.assertIsNone(streak.protocol_send_deadline(account, now=NOW))

    def test_personal_scheduled_scope_is_not_authorized_after_end_time(self):
        job = self.due(end_time="08:31")
        account = streak.account_groups({}, self.fixture.accounts, profile_key=job["key"], revision=job["revision"], claim=job["token"])[0][1][0]
        self.assertTrue(streak.within_send_window(account, now=NOW + timedelta(minutes=30)))
        self.assertFalse(streak.within_send_window(account, now=NOW + timedelta(minutes=31)))

    def test_browser_retry_uses_authorized_personal_timezone_through_disk_readback(self):
        from zoneinfo import ZoneInfo
        from core import refusal_retry, streak_state, tasks

        self.fixture.accounts[0]["targets"] = ["friend"]
        config._save_json_file(config.users_data_path(), self.fixture.accounts)
        self.save(at_time="00:30", timezone_name="Asia/Tokyo")
        now = datetime(2026, 11, 3, 23, 59, tzinfo=ZoneInfo("Asia/Tokyo"))
        attempt = now - timedelta(hours=23, minutes=29)
        persisted = config.get_userData(force_reload=True)
        streak_state.mark_failed(
            persisted[0], "friend", category="send_rejected", reason="8610",
            retryable=False, now=attempt,
        )
        persisted[0]["failure_queue"] = {"friend": {
            "category": "send_rejected", "lastAttemptAt": attempt.isoformat(timespec="seconds"),
            "serverReceipt": {
                "call": "message_send", "ok": False, "protoParsed": True,
                "protoOk": False, "protoStatusCode": 0, "protoSendStatus": 3,
                "protoCheckCode": 2, "protoCheckCodes": {
                    "status_code": 8610, "raw_check_code": 2,
                }, "rejection": {"key": "status_code", "value": 8610, "source": "raw"},
            },
        }}
        config._save_json_file(config.users_data_path(), persisted)
        scoped = streak.account_groups({}, persisted, manual=True)[0][1][0]

        with patch.dict(os.environ, {"SPARKFLOW_FALLBACK_PHASE": "1"}), patch.object(tasks, "datetime") as clock:
            clock.now.return_value = now
            forged = dict(scoped)
            forged["_personal_scope"] = dict(
                scoped["_personal_scope"], schedule_timezone="America/New_York",
            )
            self.assertFalse(tasks._mark_browser_target_in_flight(
                forged, "friend", run_id="forged-policy",
            ))
            self.assertTrue(tasks._mark_browser_target_in_flight(scoped, "friend", run_id="real-file-policy"))

        reloaded = config.get_userData(force_reload=True)
        self.assertNotIn("_personal_scope", reloaded[0])
        self.assertTrue(refusal_retry.used_today(
            reloaded[0], "friend", now, schedule_timezone=ZoneInfo("Asia/Tokyo"),
        ))

    def test_unknown_child_failure_is_visible_and_keeps_normal_next_run(self):
        job = self.due()
        due = NOW + timedelta(minutes=45)
        streak.finish_claim(job, outcome="unknown", now=due)

        profile = streak.preferences(ALICE)
        self.assertEqual("unknown", profile["last_run"]["status"])
        self.assertEqual(due.isoformat(), profile["last_run"]["at"])
        self.assertEqual("2026-11-03T00:30:00+00:00", profile["next_run_at"])
        # A same-form save and a web-user update must not erase the warning.
        self.assertEqual(profile, self.save())
        users.save_web_users(users.get_web_users())
        self.assertEqual("unknown", streak.preferences(ALICE)["last_run"]["status"])

    def test_late_unknown_child_cannot_overwrite_a_revised_plan(self):
        job = self.due()
        revised = self.save(messages=["revised plan"])
        streak.finish_claim(job, outcome="unknown", now=NOW)
        current = streak.preferences(ALICE)
        self.assertEqual(revised["revision"], current["revision"])
        self.assertEqual(["revised plan"], current["messages"])
        self.assertIsNone(current["last_run"])

    def test_failed_oserror_retries_only_within_a_durable_budget(self):
        self.save()
        now = datetime.fromisoformat(streak.preferences(ALICE)["next_run_at"])
        for attempt in range(1, streak.MAX_START_FAILURE_RETRIES + 1):
            job = streak.claim_due(now)
            self.assertIsNotNone(job)
            streak.finish_claim(job, outcome="failed", now=now)
            profile = streak.preferences(ALICE)
            self.assertEqual(attempt, profile["last_run"]["retry_count"])
            self.assertEqual(attempt < streak.MAX_START_FAILURE_RETRIES, profile["last_run"]["retry_scheduled"])
            now += timedelta(minutes=1)
        self.assertEqual("2026-11-03T00:30:00+00:00", streak.preferences(ALICE)["next_run_at"])

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
        for changes in ({"at_time": "24:00"}, {"timezone_name": "../secret"}, {"messages": ["x" * 501]}, {"messages": ["a\0b"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.save(**changes)

        with patch.object(streak, "get_config", return_value={"messageTemplate": "全局模板"}):
            saved = self.save(messages=[])
        self.assertEqual([], saved["messages"])

        with patch.object(streak, "get_config", return_value={}):
            with self.assertRaises(ValueError):
                self.save(messages=[])

    def test_account_required_and_disabled_principal_cannot_save(self):
        with self.assertRaises(ValueError):
            self.save({"username": "owner", "role": "admin"})
        people = users.get_web_users()
        people[0]["enabled"] = False
        users.save_web_users(people)
        with self.assertRaises(ValueError):
            self.save()

    def test_failed_or_unknown_run_is_shown_with_a_manual_check_instruction(self):
        job = self.due()
        streak.finish_claim(job, outcome="unknown", now=NOW)

        page = self.fixture.login().get("/preferences/streak")
        self.assertEqual(200, page.status_code)
        self.assertIn("上次计划结果不确定", page.text)
        self.assertIn("请到发送控制台核对", page.text)

    def test_form_cannot_select_other_user_or_bypass_csrf(self):
        alice = self.fixture.login()
        csrf = self.fixture.token(alice)
        form = {"csrf_token": csrf, "edit_revision": streak.preferences(ALICE)["revision"], "enabled": "on", "time": "09:15", "timezone": "Asia/Shanghai", "messages": "自己的文字", "user_id": users.get_web_users()[1]["user_id"], "username": "bob"}
        response = alice.post("/preferences/streak", data=form, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual(["自己的文字"], streak.preferences(ALICE)["messages"])
        self.assertEqual([], streak.preferences(BOB)["messages"])
        self.assertEqual(403, alice.post("/preferences/streak", data={**form, "csrf_token": "wrong"}).status_code)
        self.assertNotIn("自己的文字", self.fixture.login("bob", "Second-secret-2026!").get("/preferences/streak").text)

    def test_preferences_form_saves_and_renders_the_personal_end_time(self):
        alice = self.fixture.login()
        csrf = self.fixture.token(alice)
        form = {
            "csrf_token": csrf,
            "edit_revision": streak.preferences(ALICE)["revision"],
            "enabled": "on",
            "time": "08:30",
            "end_time": "09:17",
            "timezone": "Asia/Shanghai",
            "messages": "只发给我的账号",
        }
        response = alice.post("/preferences/streak", data=form, follow_redirects=False)

        self.assertEqual(303, response.status_code)
        self.assertEqual("09:17", streak.preferences(ALICE)["end_time"])
        page = alice.get("/preferences/streak")
        self.assertIn('name="end_time"', page.text)
        self.assertIn('name="end_time" value="09:17"', page.text)
        self.assertIn("不会展开", page.text)
        self.assertIn("填写后只用此处个人内容，不与全局模板混用", page.text)
        self.assertIn("留空使用“设置 → 消息”中已有的全局默认模板", page.text)

    def test_only_claimed_user_accounts_are_selected_for_personal_run(self):
        job = self.due()
        self.save(BOB)
        groups = streak.account_groups({}, self.fixture.accounts, profile_key=job["key"], revision=job["revision"], claim=job["token"])
        self.assertEqual(["a1"], [a["account_ref"] for _, accounts in groups for a in accounts])

    def test_dispatcher_marks_nonbusy_child_exit_unknown_without_retrying(self):
        for code in (1, -9):
            with self.subTest(code=code):
                job = {"key": "alice", "revision": "r", "token": str(code)}
                child = Mock()
                child.poll.return_value = code
                with patch("core.personal_dispatcher.claim_due", side_effect=[job, None]), patch("core.personal_dispatcher.subprocess.Popen", return_value=child), patch("core.personal_dispatcher.finish_claim") as finish:
                    dispatcher = PersonalDispatcher()
                    dispatcher.tick()
                    dispatcher.tick()
                finish.assert_called_once_with(job, outcome="unknown")

    def test_dispatcher_marks_oserror_as_failed_before_child_and_retries_boundedly(self):
        job = self.due()
        with patch("core.personal_dispatcher.claim_due", return_value=job), patch("core.personal_dispatcher.subprocess.Popen", side_effect=OSError("no process")), patch("core.personal_dispatcher.finish_claim") as finish:
            with self.assertRaises(OSError):
                PersonalDispatcher().tick()
        finish.assert_called_once_with(job, outcome="failed")

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
