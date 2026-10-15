"""Daily account patrol: A7-A14 of send-failure-visibility-and-patrol.

No real browser runs here: the page, context, lock and notification channel are
fakes, and the account store is a temporary file.
"""
import asyncio
import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import main as main_module
from core import browser as browser_module
from core import friend_identity, notifications, patrol, streak_state, tasks
from core import identity as account_identity
from core.friends import CHAT_PAGE_URL, FriendRefreshError, FriendScanResult
from scripts.cron_runner import line_should_run
from utils import config as config_module
from utils import notify
from webui import app as app_module
from webui import ops


TZ8 = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 14, 9, 30, tzinfo=TZ8)
TODAY_ISO = NOW.astimezone(timezone.utc).isoformat(timespec="seconds")
ACCOUNT_ID = "1234567890"
ACCOUNT_UID = "88000000000001234"
UID_MATCHA = "73001234567891001"
UID_BOB = "73001234567892002"
USER_DETAIL_URL = "https://creator.douyin.com/aweme/v1/creator/im/user_detail/?aid=2906"
USER_TOKEN_URL = "https://creator.douyin.com/aweme/v1/creator/im/user_token/?aid=2906"


def isolate_account_data(test_case, accounts):
    temp_dir = tempfile.TemporaryDirectory()
    test_case.addCleanup(temp_dir.cleanup)
    path = Path(temp_dir.name) / "usersData.json"
    path.write_text(json.dumps(accounts, ensure_ascii=False), encoding="utf-8")
    patches = [
        patch.object(config_module, "get_environment", return_value=config_module.Environment.LOCAL),
        patch.object(config_module, "users_data_path", return_value=path),
    ]
    for item in patches:
        item.start()
        test_case.addCleanup(item.stop)
    cached = config_module.userData
    config_module.userData = None
    test_case.addCleanup(lambda: setattr(config_module, "userData", cached))
    return path


def stored(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))[0]


def user_detail(*pairs):
    return {
        "status_code": 0,
        "user_list": [
            {"user_id": uid, "user": {"nickname": name, "SecretUseId": f"sec-{uid[-4:]}"}}
            for uid, name in pairs
        ],
    }


def base_account(**extra):
    account = {
        "account_ref": "acc-1",
        "unique_id": ACCOUNT_ID,
        "username": "主号",
        "enabled": True,
        "cookies": [{"name": "sessionid", "value": "v", "domain": ".douyin.com"}],
        "targets": ["抹茶叔叔", "Bob"],
        "target_uid_bindings": {
            "抹茶叔叔": {"uid": UID_MATCHA, "secUid": "", "source": "web"},
            "Bob": {"uid": UID_BOB, "secUid": "", "source": "web"},
        },
        "target_states": {
            "Bob": {
                "targetRef": "Bob",
                "displayName": "Bob",
                "status": "failed_retryable",
                "lastAttemptAt": TODAY_ISO,
                "lastErrorCategory": "browser_timeout",
                "attemptCount": 2,
            }
        },
        "failure_queue": {
            "Bob": {"category": "browser_timeout", "lastAttemptAt": TODAY_ISO, "attemptCount": 2}
        },
    }
    account.update(extra)
    return account


class FakeResponse:
    def __init__(self, url, body):
        self.url = url
        self._body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

    async def json(self):
        return json.loads(self._body)

    async def text(self):
        return self._body


class RecordingLocator:
    """Any interaction with page elements is recorded, never performed."""

    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    async def count(self):
        return 0

    async def is_visible(self, timeout=None):
        return False

    def nth(self, index):
        return self

    async def click(self, *args, **kwargs):
        self.page.actions.append(("click", self.selector))

    async def type(self, *args, **kwargs):
        self.page.actions.append(("type", self.selector))

    async def fill(self, *args, **kwargs):
        self.page.actions.append(("fill", self.selector))

    async def press(self, *args, **kwargs):
        self.page.actions.append(("press", self.selector))


class RecordingKeyboard:
    def __init__(self, page):
        self.page = page

    async def type(self, *args, **kwargs):
        self.page.actions.append(("keyboard.type", args))

    async def press(self, *args, **kwargs):
        self.page.actions.append(("keyboard.press", args))


class FakePage:
    def __init__(self):
        self.handlers = {}
        self.gotos = []
        self.actions = []
        self.url = "about:blank"
        self.closed = False
        self.keyboard = RecordingKeyboard(self)

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers.get(event, []).remove(handler)

    def emit(self, response):
        for handler in list(self.handlers.get("response", [])):
            handler(response)

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url

    def locator(self, selector):
        return RecordingLocator(self, selector)

    def get_by_text(self, text, **kwargs):
        return RecordingLocator(self, f"text={text}")

    def get_by_role(self, role, **kwargs):
        return RecordingLocator(self, f"role={role}")

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def set_default_navigation_timeout(self, value):
        pass

    def set_default_timeout(self, value):
        pass

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class PatrolHarness:
    """Wire a fake browser session into core.patrol for one test."""

    def __init__(
        self,
        test_case,
        *,
        names=("抹茶", "Bob"),
        complete=True,
        detail=((UID_MATCHA, "抹茶"), (UID_BOB, "Bob")),
        login_uid=ACCOUNT_UID,
        login_prompt=False,
        read_error=None,
    ):
        self.page = FakePage()
        self.context = FakeContext(self.page)
        self.sessions = 0
        self.sent = MagicMock(return_value={"webhook": True})
        lock_dir = tempfile.TemporaryDirectory()
        state_dir = tempfile.TemporaryDirectory()
        test_case.addCleanup(lock_dir.cleanup)
        test_case.addCleanup(state_dir.cleanup)
        self.state_path = Path(state_dir.name) / "notify_state.json"

        async def open_session(user, network_mode, profile_config):
            self.sessions += 1
            return None, None, self.context

        async def detect_login_prompt(page):
            return (True, "visible selector .login-mask") if login_prompt else (False, "")

        async def collect(page, on_progress=None):
            if read_error is not None:
                raise read_error
            if detail is not None:
                page.emit(FakeResponse(USER_DETAIL_URL, user_detail(*detail)))
            if login_uid:
                page.emit(FakeResponse(USER_TOKEN_URL, {"status_code": 0, "user_id": login_uid}))
            page.emit(FakeResponse("https://creator.douyin.com/other/", {}))
            return FriendScanResult(list(names), complete=complete, reason="" if complete else "列表滚动没有继续推进")

        patches = [
            patch.object(patrol, "_open_session", side_effect=open_session),
            patch.object(patrol, "_detect_login_prompt", side_effect=detect_login_prompt),
            patch.object(patrol, "collect_friend_names", side_effect=collect),
            patch.object(patrol, "douyin_network_modes", return_value=["direct"]),
            patch.object(patrol, "PAGE_SETTLE_SECONDS", 0),
            patch.object(browser_module, "BROWSER_ACCOUNT_LOCK_DIR", lock_dir.name),
            patch.object(patrol, "get_config", return_value={"notifications": {"enabled": True}}),
            patch.object(notifications, "NOTIFY_STATE_PATH", self.state_path),
            patch.object(notify, "enabled_channel_names", return_value=["webhook"]),
            patch.object(notify, "send_message", self.sent),
        ]
        for item in patches:
            item.start()
            test_case.addCleanup(item.stop)

    def run(self, refs=None, **kwargs):
        return asyncio.run(patrol.run_patrol(refs, now=NOW, **kwargs))

    def messages(self):
        return [call.args[0] for call in self.sent.call_args_list]


def bound_identity(account):
    account[account_identity.FIELD_UID] = ACCOUNT_UID
    account[account_identity.FIELD_BINDING] = {"source": account_identity.IDENTITY_SOURCE, "version": 1}
    return account


class LoginFailurePatrolTests(unittest.TestCase):
    """A9."""

    def setUp(self):
        self.path = isolate_account_data(self, [base_account()])

    def test_login_prompt_writes_account_health_and_pushes_once(self):
        harness = PatrolHarness(self, login_prompt=True)
        before = stored(self.path)

        result = harness.run()
        harness.run()

        account = stored(self.path)
        self.assertEqual("login_failed", result["accounts"][0]["conclusion"])
        health = account["account_health"]
        self.assertIs(False, health["healthy"])
        self.assertEqual("login_required", health["category"])
        self.assertEqual("patrol", health["source"])
        self.assertTrue(health["checkedAt"])
        self.assertEqual("login_required", account["patrol"]["login"]["category"])
        self.assertIs(False, account["patrol"]["login"]["ok"])
        # Target state and failure counters are untouched.
        self.assertEqual(before_counts(before), before_counts(account))
        # One push for two patrols on the same day.
        self.assertEqual(1, harness.sent.call_count)
        text = harness.messages()[0]["text"]
        self.assertIn("主号", text)
        self.assertIn("登录态失效", text)
        self.assertNotIn("sessionid", text)

    def test_following_preflight_skips_the_account_and_relogin_restores_it(self):
        harness = PatrolHarness(self, login_prompt=True)
        harness.run()
        account = stored(self.path)

        preflight = streak_state.preflight_account(account, NOW)

        self.assertFalse(preflight["healthy"])
        self.assertEqual("login_required", preflight["category"])
        account_identity.clear_login_health(account)
        self.assertTrue(streak_state.preflight_account(account, NOW)["healthy"])

    def test_scheduled_run_skips_account_and_does_not_repeat_the_login_push(self):
        harness = PatrolHarness(self, login_prompt=True)
        harness.run()
        account = stored(self.path)

        with (
            patch.object(tasks, "_is_manual_run", return_value=False),
            patch.object(tasks, "_is_fallback_run", return_value=False),
            patch.object(tasks, "datetime", wraps=datetime) as fake_datetime,
        ):
            fake_datetime.now.side_effect = lambda tz=None: NOW if tz is None else NOW.astimezone(tz)
            runnable = tasks._prepare_active_users_for_run(
                {"dailySendWindow": {"enabled": False}}, [dict(account)]
            )
        self.assertEqual([], runnable)

        after = stored(self.path)
        notifications.notify_run_finished(
            "run-1",
            "skipped_no_runnable_accounts",
            {"notifications": {"enabled": True}},
            [after],
            schedule_tz=TZ8,
            send_window={"enabled": True},
            manual_run=True,
            now=NOW + timedelta(minutes=40),
            state_path=harness.state_path,
        )
        self.assertEqual(1, harness.sent.call_count)
        self.assertEqual(before_counts(account), before_counts(stored(self.path)))

    def test_passing_patrol_lifts_its_own_failure(self):
        harness = PatrolHarness(self, login_prompt=True)
        harness.run()
        PatrolHarness(self).run()

        account = stored(self.path)
        self.assertNotIn("patrol_login_failure", account)
        self.assertIs(True, account["account_health"]["healthy"])
        self.assertTrue(streak_state.preflight_account(account, NOW)["healthy"])

    def test_identity_mismatch_is_a_login_failure_without_full_uids(self):
        path = isolate_account_data(self, [bound_identity(base_account())])
        harness = PatrolHarness(self, login_uid="88000000000009999")

        harness.run()

        account = stored(path)
        self.assertEqual("account_identity_mismatch", account["account_health"]["category"])
        self.assertEqual("account_identity_mismatch", streak_state.preflight_account(account, NOW)["category"])
        reason = account["patrol"]["login"]["reason"]
        self.assertIn("1234", reason)
        self.assertNotIn(ACCOUNT_UID, reason)
        self.assertNotIn("88000000000009999", reason)
        self.assertNotIn(ACCOUNT_UID, json.dumps(harness.messages(), ensure_ascii=False))

    def test_missing_cookies_fail_without_opening_a_browser(self):
        path = isolate_account_data(self, [base_account(cookies=[])])
        harness = PatrolHarness(self)

        harness.run()

        self.assertEqual(0, harness.sessions)
        self.assertEqual("missing_cookies", stored(path)["account_health"]["category"])


def before_counts(account):
    """Send status and failure counters, independent of the ledger key.

    A patrol may migrate a nickname-keyed ledger to the target's stable uid key
    (core.friend_identity); the values themselves must not change.
    """
    states = sorted(
        (str(state.get("status")), int(state.get("attemptCount") or 0), str(state.get("lastAttemptAt") or ""))
        for state in (account.get("target_states") or {}).values()
        if state.get("status") not in (None, "pending")
    )
    queue = sorted(
        (str(entry.get("category")), int(entry.get("attemptCount") or 0))
        for entry in (account.get("failure_queue") or {}).values()
    )
    return {"states": states, "queue": queue}


class ReadOnlyPatrolTests(unittest.TestCase):
    """A13 and the target comparison."""

    def setUp(self):
        self.path = isolate_account_data(self, [bound_identity(base_account())])

    def test_patrol_never_opens_a_chat_or_sends(self):
        harness = PatrolHarness(self)
        before = stored(self.path)

        result = harness.run()

        self.assertEqual([CHAT_PAGE_URL], harness.page.gotos)
        self.assertEqual([], harness.page.actions)
        self.assertTrue(harness.page.closed)
        self.assertTrue(harness.context.closed)
        self.assertEqual([], harness.page.handlers.get("response"))
        account = stored(self.path)
        self.assertEqual(before_counts(before), before_counts(account))
        self.assertNotIn("message_history", account)
        self.assertEqual("verified", result["accounts"][0]["login"]["identity"])

    def test_rename_is_followed_recorded_and_pushed_once(self):
        harness = PatrolHarness(self)

        harness.run()

        account = stored(self.path)
        self.assertIn("抹茶", account["targets"])
        self.assertNotIn("抹茶叔叔", account["targets"])
        rows = {row["target"]: row for row in account["patrol"]["targets"]}
        self.assertEqual("renamed", rows["抹茶"]["status"])
        self.assertEqual("抹茶叔叔", rows["抹茶"]["oldName"])
        self.assertEqual("抹茶", rows["抹茶"]["newName"])
        self.assertEqual("found", rows["Bob"]["status"])
        self.assertTrue(account["patrol"]["completed"])
        self.assertEqual(["抹茶", "Bob"], account["friends_cache"])
        # Bob's failure state is untouched by the patrol.
        self.assertEqual([("failed_retryable", 2, TODAY_ISO)], before_counts(account)["states"])
        harness.run()
        self.assertEqual("found", {row["target"]: row for row in stored(self.path)["patrol"]["targets"]}["抹茶"]["status"])
        self.assertEqual(1, harness.sent.call_count)
        text = harness.messages()[0]["text"]
        self.assertIn("「抹茶叔叔」→「抹茶」", text)
        self.assertNotIn(UID_MATCHA, text)
        self.assertIn(UID_MATCHA[-4:], text)

    def test_missing_target_needs_a_complete_read(self):
        harness = PatrolHarness(self, names=("Bob",), detail=((UID_BOB, "Bob"),), complete=False)

        harness.run()

        account = stored(self.path)
        self.assertFalse(account["patrol"]["completed"])
        self.assertEqual("incomplete", account["patrol"]["conclusion"])
        self.assertIn("读取不完整", account["patrol"]["reason"])
        self.assertNotIn("friends_cache", account)
        self.assertNotIn("missing", {row["status"] for row in account["patrol"]["targets"]})
        self.assertEqual(0, harness.sent.call_count)

        complete = PatrolHarness(self, names=("Bob",), detail=((UID_BOB, "Bob"),))
        complete.run()

        account = stored(self.path)
        rows = {row["target"]: row["status"] for row in account["patrol"]["targets"]}
        self.assertEqual("missing", rows["抹茶叔叔"])
        self.assertEqual(["抹茶叔叔", "Bob"], account["targets"])
        self.assertEqual(1, complete.sent.call_count)
        self.assertIn("目标缺失", complete.messages()[0]["text"])

    def test_no_problem_means_no_push(self):
        path = isolate_account_data(
            self, [bound_identity(base_account(targets=["Bob"], target_uid_bindings={}))]
        )
        harness = PatrolHarness(self, names=("Bob",), detail=((UID_BOB, "Bob"),))

        result = harness.run()

        self.assertEqual("ok", result["accounts"][0]["conclusion"])
        self.assertEqual(0, harness.sent.call_count)
        self.assertIs(True, stored(path)["account_health"]["healthy"])

    def test_page_failure_is_incomplete_and_not_a_login_verdict(self):
        harness = PatrolHarness(
            self, read_error=FriendRefreshError("chat page did not load within timeout", category="structure_changed")
        )

        harness.run()

        account = stored(self.path)
        self.assertIsNone(account["patrol"]["login"]["ok"])
        self.assertEqual("incomplete", account["patrol"]["conclusion"])
        self.assertNotIn("account_health", account)
        self.assertEqual(0, harness.sent.call_count)

    def test_disabled_accounts_are_not_patrolled_and_refs_filter(self):
        path = isolate_account_data(
            self,
            [
                base_account(),
                base_account(account_ref="acc-2", unique_id="222", username="停用号", enabled=False),
                base_account(account_ref="acc-3", unique_id="333", username="三号"),
            ],
        )
        harness = PatrolHarness(self)

        everything = harness.run()
        only = harness.run(["acc-3"])

        self.assertEqual(["主号", "三号"], [item["account"] for item in everything["accounts"]])
        self.assertEqual(["三号"], [item["account"] for item in only["accounts"]])
        self.assertNotIn("patrol", json.loads(Path(path).read_text(encoding="utf-8"))[1])


class PatrolLockTests(unittest.TestCase):
    """A11."""

    def setUp(self):
        self.path = isolate_account_data(self, [base_account()])

    def test_busy_account_is_skipped_with_a_reason(self):
        harness = PatrolHarness(self)
        waits = []

        async def busy(user, account_name, *, wait_seconds, poll_seconds, **kwargs):
            waits.append(wait_seconds)
            raise RuntimeError(f"timed out waiting for browser account lock for {account_name}")

        with patch.object(patrol, "acquire_browser_account_lock", side_effect=busy):
            result = harness.run(lock_wait=3)

        self.assertEqual([3], waits)
        self.assertEqual(0, harness.sessions)
        record = stored(self.path)["patrol"]
        self.assertEqual("skipped", record["conclusion"])
        self.assertIn("占用", record["reason"])
        self.assertNotIn("account_health", stored(self.path))
        self.assertEqual("skipped", result["accounts"][0]["conclusion"])
        self.assertEqual(0, harness.sent.call_count)

    def test_patrol_holds_the_shared_account_lock_but_not_the_run_lock(self):
        harness = PatrolHarness(self)
        observed = {}
        original = harness.context.new_page
        run_lock_dir = tempfile.TemporaryDirectory()
        self.addCleanup(run_lock_dir.cleanup)

        async def new_page():
            lock_dir = Path(browser_module.BROWSER_ACCOUNT_LOCK_DIR)
            observed["locks"] = sorted(path.name for path in lock_dir.glob("*.lock"))
            # A send run can still take the global task lock while patrolling.
            with tasks.task_run_lock(Path(run_lock_dir.name) / "task.run.lock"):
                observed["run_lock"] = True
            return await original()

        harness.context.new_page = new_page
        harness.run()

        self.assertEqual([f"{browser_module.browser_account_lock_name(base_account())}.lock"], observed["locks"])
        self.assertTrue(observed["run_lock"])
        self.assertEqual([], list(Path(browser_module.BROWSER_ACCOUNT_LOCK_DIR).glob("*.lock")))

    def test_sender_waits_for_the_patrol_lock_instead_of_failing(self):
        lock_dir = tempfile.TemporaryDirectory()
        self.addCleanup(lock_dir.cleanup)
        account = base_account()

        async def scenario():
            held = await browser_module.acquire_browser_account_lock(
                account, "patrol", lock_dir=lock_dir.name
            )
            with self.assertRaises(RuntimeError):
                # A short wait times out while the patrol holds the lock...
                await browser_module.acquire_browser_account_lock(
                    account, "sender", lock_dir=lock_dir.name, wait_seconds=0, poll_seconds=0.01
                )
            browser_module.release_browser_account_lock(*held, "patrol")
            # ...and succeeds once the patrol released it.
            again = await browser_module.acquire_browser_account_lock(
                account, "sender", lock_dir=lock_dir.name, wait_seconds=1, poll_seconds=0.01
            )
            browser_module.release_browser_account_lock(*again, "sender")

        asyncio.run(scenario())


class PatrolNotificationTests(unittest.TestCase):
    """A14, A7, A8."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.state_path = Path(temp_dir.name) / "notify_state.json"
        self.sent = MagicMock(return_value={"webhook": True})
        for item in (
            patch.object(notify, "enabled_channel_names", return_value=["webhook"]),
            patch.object(notify, "send_message", self.sent),
        ):
            item.start()
            self.addCleanup(item.stop)

    def _account(self, events):
        return {"account_ref": "acc-1", "username": "主号", "enabled": True, "targets": ["抹茶"], "identity_events": events}

    def _event(self, category, **extra):
        event = {
            "key": f"2026-10-14|{category}|uid:abc",
            "date": "2026-10-14",
            "category": category,
            "identity": "uid:abc",
            "targetName": "抹茶",
            "oldName": "抹茶叔叔" if category == "renamed" else "",
            "newName": "抹茶" if category == "renamed" else "",
            "uidSuffix": "1001",
        }
        event.update(extra)
        return event

    def _run_finished(self, users, now=NOW):
        return notifications.notify_run_finished(
            "run",
            "completed",
            {"notifications": {"enabled": True}},
            users,
            schedule_tz=TZ8,
            send_window={"enabled": True},
            manual_run=True,
            now=now,
            state_path=self.state_path,
        )

    def test_send_run_pushes_rename_events_once_even_after_restart(self):
        users = [self._account([self._event("renamed")])]

        first = self._run_finished(users)
        # A restart only keeps the state file.
        second = self._run_finished(users, now=NOW + timedelta(hours=2))

        self.assertTrue(first["identity"])
        self.assertFalse(second["identity"])
        self.assertEqual(1, self.sent.call_count)
        message = self.sent.call_args.args[0]
        self.assertEqual("identity", message["event"])
        self.assertIn("「抹茶叔叔」→「抹茶」", message["text"])
        self.assertIn("1001", message["text"])

    def test_missing_from_send_scan_and_patrol_share_one_push(self):
        users = [self._account([self._event("missing")])]
        self._run_finished(users)

        pushed = notifications.notify_patrol_finished(
            [{"accountRef": "acc-1", "account": "主号", "login": {"ok": True}}],
            {"notifications": {"enabled": True}},
            users,
            schedule_tz=TZ8,
            now=NOW + timedelta(minutes=5),
            state_path=self.state_path,
        )

        self.assertFalse(pushed)
        self.assertEqual(1, self.sent.call_count)

    def test_older_events_and_disabled_accounts_are_not_pushed(self):
        old = self._event("missing", date="2026-10-13", key="2026-10-13|missing|uid:abc")
        disabled = dict(self._account([self._event("renamed")]), enabled=False)

        result = self._run_finished([self._account([old]), disabled])

        self.assertFalse(result["identity"])
        self.sent.assert_not_called()

    def test_push_failure_does_not_change_the_patrol(self):
        self.sent.side_effect = RuntimeError("channel down")

        pushed = notifications.notify_patrol_finished(
            [{"accountRef": "acc-1", "account": "主号", "login": {"ok": False, "category": "login_required"}}],
            {"notifications": {"enabled": True}},
            [self._account([])],
            schedule_tz=TZ8,
            now=NOW,
            state_path=self.state_path,
        )

        self.assertFalse(pushed)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        # The key was reserved before sending: no retry storm after a failure.
        self.assertIn("2026-10-14|acc-1|-|login_required", state["dedupe"])

    def test_no_channel_means_no_push_and_no_state(self):
        with patch.object(notify, "enabled_channel_names", return_value=[]):
            pushed = notifications.notify_patrol_finished(
                [{"accountRef": "acc-1", "account": "主号", "login": {"ok": False, "category": "login_required"}}],
                {},
                [self._account([])],
                schedule_tz=TZ8,
                now=NOW,
                state_path=self.state_path,
            )
        self.assertFalse(pushed)
        self.assertFalse(self.state_path.exists())

    def test_patrol_push_does_not_send_a_daily_summary(self):
        notifications.notify_patrol_finished(
            [{"accountRef": "acc-1", "account": "主号", "login": {"ok": False, "category": "login_required"}}],
            {"notifications": {"enabled": True}},
            [self._account([self._event("renamed")])],
            schedule_tz=TZ8,
            now=NOW,
            state_path=self.state_path,
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual({}, state["summaries"])
        message = self.sent.call_args.args[0]
        self.assertEqual("patrol", message["event"])
        self.assertIn("登录态失效", message["text"])
        self.assertIn("已自动跟随", message["text"])


class PatrolCronTests(unittest.TestCase):
    """A10."""

    def setUp(self):
        for item in (
            patch.object(ops, "running_in_container", return_value=True),
        ):
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def _patrol_lines(text):
        return [line for line in text.splitlines() if ops.is_patrol_schedule_line(line)]

    def test_window_start_ten_patrols_at_nine_thirty(self):
        crontab = ops.replace_douyin_cron_schedule("", "10:00-18:00/20m")

        lines = self._patrol_lines(crontab)
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("30 9 * * * "))
        self.assertIn("SPARKFLOW_TRIGGER_LABEL='account patrol'", lines[0])
        self.assertIn("bash /app/scripts/run_scheduled_task.sh", lines[0])
        self.assertTrue(lines[0].endswith(f">> {ops.CRON_LOG_PATH} 2>&1"))
        self.assertEqual("patrol", ops.schedule_line_kind(lines[0]))
        self.assertTrue(line_should_run(lines[0], datetime(2026, 10, 14, 9, 30))[0])
        self.assertFalse(line_should_run(lines[0], datetime(2026, 10, 14, 10, 0))[0])

    def test_midnight_window_patrols_the_evening_before(self):
        crontab = ops.replace_douyin_cron_schedule("", "00:00-08:00/20m")
        self.assertTrue(self._patrol_lines(crontab)[0].startswith("30 23 * * * "))

    def test_fixed_time_patrols_thirty_minutes_before_it(self):
        self.assertTrue(
            self._patrol_lines(ops.replace_douyin_cron_schedule("", "08:15"))[0].startswith("45 7 * * * ")
        )
        self.assertTrue(
            self._patrol_lines(ops.replace_douyin_cron_schedule("", "00:10"))[0].startswith("40 23 * * * ")
        )

    def test_no_scheduled_send_means_no_patrol(self):
        self.assertIsNone(ops.patrol_time_for_schedule(None))
        self.assertEqual("", ops.patrol_cron_line({}))

    def test_resaving_replaces_the_patrol_line_instead_of_adding_one(self):
        first = ops.replace_douyin_cron_schedule("MAILTO=\"\"\n", "10:00-18:00/20m")
        second = ops.replace_douyin_cron_schedule(first, "08:00-20:00/20m")

        lines = self._patrol_lines(second)
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("30 7 * * * "))
        self.assertIn('MAILTO=""', second)

    def test_saving_the_window_updates_the_spool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cron_path = Path(temp_dir) / "root"
            old = ops.replace_douyin_cron_schedule("", "10:00-18:00/20m")
            with (
                patch.object(ops, "HOST_CRONTAB_PATH", cron_path),
                patch.object(ops, "persist_schedule_config"),
                patch.object(ops, "read_crontab", return_value=old),
            ):
                result = ops.update_daily_schedule("12:00-20:00/20m")
            text = cron_path.read_text(encoding="utf-8")

        self.assertEqual(0, result.returncode)
        lines = self._patrol_lines(text)
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("30 11 * * * "))

    def test_current_schedule_ignores_the_patrol_line(self):
        crontab = ops.replace_douyin_cron_schedule("", "18:20")
        # Put the patrol first, as a hand-edited spool might.
        reordered = "\n".join(sorted(crontab.splitlines(), key=lambda line: not ops.is_patrol_schedule_line(line)))
        with (
            patch.object(ops, "get_config", return_value={"dailySendWindow": {"enabled": False}}),
            patch.object(ops, "read_crontab", return_value=reordered),
        ):
            self.assertEqual("18:20", ops.current_daily_schedule())

    def test_alignment_recognises_the_patrol_line(self):
        window = {"enabled": True, "startHour": 10, "endHour": 18, "scheduleIntervalMinutes": 20}
        full = ops.replace_douyin_cron_schedule("", "10:00-18:00/20m")
        without_patrol = "\n".join(
            line for line in full.splitlines() if not ops.is_patrol_schedule_line(line)
        )
        with patch.object(ops, "get_config", return_value={"dailySendWindow": window}):
            with patch.object(ops, "read_crontab", return_value=full):
                aligned = ops.get_schedule_alignment(recent_triggers=[])
            with patch.object(ops, "read_crontab", return_value=without_patrol):
                missing = ops.get_schedule_alignment(recent_triggers=[])

        self.assertTrue(aligned["aligned"], aligned["detail"])
        self.assertEqual(["window"], aligned["kinds"])
        self.assertEqual("09:30", aligned["patrolExpected"])
        self.assertTrue(aligned["patrolAligned"])
        self.assertFalse(missing["aligned"])
        self.assertIn("缺少每日巡查任务行", missing["detail"])

    def test_sync_adds_patrol_to_a_fixed_schedule_once(self):
        fixed_only = "\n".join(
            line
            for line in ops.replace_douyin_cron_schedule("", "18:20").splitlines()
            if not ops.is_patrol_schedule_line(line)
        ) + "\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            cron_path = Path(temp_dir) / "root"
            cron_path.write_text(fixed_only, encoding="utf-8")
            read = lambda: cron_path.read_text(encoding="utf-8")  # noqa: E731
            with (
                patch.object(ops, "HOST_CRONTAB_PATH", cron_path),
                patch.object(ops, "read_crontab", side_effect=read),
                patch.object(ops, "get_config", return_value={"dailySendWindow": {"enabled": False}}),
            ):
                ops.sync_daily_schedule_from_config()
                once = read()
                ops.sync_daily_schedule_from_config()
                twice = read()

        self.assertEqual(once, twice)
        lines = self._patrol_lines(once)
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("50 17 * * * "))
        self.assertEqual(1, sum(1 for line in once.splitlines() if line.startswith("20 18 ")))

    def test_sync_drops_a_patrol_line_without_any_send(self):
        lonely = self._patrol_lines(ops.replace_douyin_cron_schedule("", "18:20"))[0] + "\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            cron_path = Path(temp_dir) / "root"
            cron_path.write_text(lonely, encoding="utf-8")
            with (
                patch.object(ops, "HOST_CRONTAB_PATH", cron_path),
                patch.object(ops, "read_crontab", side_effect=lambda: cron_path.read_text(encoding="utf-8")),
                patch.object(ops, "get_config", return_value={"dailySendWindow": {"enabled": False}}),
            ):
                ops.sync_daily_schedule_from_config()
            self.assertEqual([], self._patrol_lines(cron_path.read_text(encoding="utf-8")))

    def test_task_entry_runs_the_patrol_for_patrol_mode(self):
        calls = []
        with (
            patch.object(main_module, "run_patrol_cli", side_effect=lambda refs: calls.append(("patrol", refs))),
            patch.dict(os.environ, {"SPARKFLOW_TASK_MODE": "patrol"}),
        ):
            main_module.run(main_module.build_parser().parse_args(["--doTask"]))
        with patch.object(main_module, "run_patrol_cli", side_effect=lambda refs: calls.append(("patrol", refs))):
            main_module.run(main_module.build_parser().parse_args(["--patrol", "--account", "acc-1,acc-2"]))
        self.assertEqual([("patrol", None), ("patrol", ["acc-1", "acc-2"])], calls)


class PatrolWebTests(unittest.TestCase):
    """A12."""

    ADMIN = {"username": "admin", "role": "admin", "account_refs": [], "session_id": "s"}
    OTHER = {"username": "other", "role": "user", "account_refs": ["acc-9"], "session_id": "o"}
    OWNER = {"username": "owner", "role": "user", "account_refs": ["acc-1"], "session_id": "w"}

    def setUp(self):
        account = base_account(
            patrol={
                "checkedAt": TODAY_ISO,
                "trigger": "scheduled",
                "completed": True,
                "conclusion": "problems",
                "login": {"ok": True, "category": "", "reason": "", "identity": "verified"},
                "targets": [
                    {"target": "抹茶", "status": "renamed", "oldName": "抹茶叔叔", "newName": "抹茶", "reason": ""},
                    {"target": "Bob", "status": "missing", "reason": "uid_not_in_complete_list"},
                    {"target": "Carol", "status": "unbound", "reason": "ambiguous_nickname"},
                ],
                "reason": "",
                "uidDataTrusted": True,
                "friendCount": 18,
            }
        )
        self.path = isolate_account_data(self, [account])
        self.client = TestClient(app_module.app)
        self.spawned = []
        self.addCleanup(lambda: app_module._patrol_active.discard(ACCOUNT_ID))
        spawn = patch.object(app_module, "_start_background", side_effect=self.spawned.append)
        spawn.start()
        self.addCleanup(spawn.stop)
        lock = patch.object(app_module, "task_run_lock_status", return_value={"running": False})
        self.lock = lock.start()
        self.addCleanup(lock.stop)

    def _as(self, principal, csrf=True):
        stack = [
            patch.object(app_module, "current_user", return_value=principal["username"] if principal else None),
            patch.object(app_module, "current_principal", return_value=principal),
            patch.object(app_module, "validate_csrf", return_value=csrf),
        ]
        for item in stack:
            item.start()
            self.addCleanup(item.stop)

    def _post(self):
        return self.client.post(f"/accounts/{ACCOUNT_ID}/patrol", data={"csrf_token": "t"})

    def test_requires_login_and_csrf(self):
        self._as(None)
        self.assertEqual(401, self._post().status_code)

    def test_rejects_bad_csrf(self):
        self._as(self.ADMIN, csrf=False)
        self.assertEqual(403, self._post().status_code)
        self.assertEqual([], self.spawned)

    def test_other_users_account_is_forbidden_like_the_friend_refresh(self):
        self._as(self.OTHER)
        patrol_response = self._post()
        refresh_response = self.client.post(f"/accounts/{ACCOUNT_ID}/friends/refresh/async", data={"csrf_token": "t"})
        self.assertEqual(403, patrol_response.status_code)
        self.assertEqual(refresh_response.status_code, patrol_response.status_code)
        self.assertEqual(403, self.client.get(f"/accounts/{ACCOUNT_ID}/patrol/status").status_code)
        self.assertEqual([], self.spawned)

    def test_owner_starts_an_async_patrol_and_reads_its_status(self):
        self._as(self.OWNER)
        calls = []

        async def fake_run(refs, **kwargs):
            calls.append((refs, kwargs))
            return {"accounts": [{"conclusion": "ok", "reason": ""}]}

        response = self._post()

        self.assertEqual(202, response.status_code, response.text)
        self.assertEqual("running", self.client.get(f"/accounts/{ACCOUNT_ID}/patrol/status").json()["state"])
        self.assertEqual(1, len(self.spawned))
        with patch.object(app_module.account_patrol, "run_patrol", side_effect=fake_run):
            asyncio.run(self.spawned[0])
        self.assertEqual([(["acc-1"], {"trigger": "manual"})], calls)
        status = self.client.get(f"/accounts/{ACCOUNT_ID}/patrol/status").json()
        self.assertEqual("done", status["state"])
        self.assertEqual("正常", status["conclusionLabel"])
        self.assertTrue(status["latest"]["has"])
        self.assertNotIn(ACCOUNT_ID, app_module._patrol_active)

    def test_busy_protection(self):
        self._as(self.ADMIN)
        self.assertEqual(202, self._post().status_code)
        again = self._post()
        self.assertEqual(429, again.status_code)
        self.assertEqual("busy", again.json()["category"])
        self.assertEqual(1, len(self.spawned))
        self.spawned[0].close()

    def test_send_run_in_progress_is_refused(self):
        self._as(self.ADMIN)
        self.lock.return_value = {"running": True}
        response = self._post()
        self.assertEqual(409, response.status_code)
        self.assertEqual([], self.spawned)

    def test_accounts_page_shows_the_latest_patrol(self):
        self._as(self.ADMIN)
        with patch.object(app_module, "get_web_users", return_value=[]):
            page = self.client.get("/accounts").text

        self.assertIn("账号巡查", page)
        self.assertIn("立即巡查", page)
        self.assertIn(f'data-patrol-url="/accounts/{ACCOUNT_ID}/patrol"', page)
        self.assertIn("发现问题", page)
        self.assertIn("抹茶叔叔 → 抹茶", page)
        self.assertIn('data-patrol-target="missing"', page)
        self.assertIn("未绑定 uid", page)
        self.assertIn("读到 18 个好友", page)


class PatrolViewTests(unittest.TestCase):
    def test_login_failure_and_incomplete_reason_are_listed(self):
        view = patrol.patrol_view(
            {
                "patrol": {
                    "checkedAt": TODAY_ISO,
                    "conclusion": "login_failed",
                    "completed": False,
                    "login": {"ok": False, "category": "login_required", "reason": "x"},
                    "targets": [],
                    "reason": "页面出现登录提示",
                }
            }
        )
        self.assertEqual("登录态失效", view["conclusionLabel"])
        self.assertEqual("danger", view["pill"])
        self.assertTrue(any("登录" in issue for issue in view["issues"]))
        self.assertEqual("09:30", view["checkedDisplay"][-5:])
        self.assertFalse(patrol.patrol_view({})["has"])


class PatrolCancellationAndPushTests(unittest.TestCase):
    def test_cancellation_during_context_close_still_stops_playwright(self):
        events = []
        close_started = asyncio.Event()
        allow_close_finish = asyncio.Event()

        context = MagicMock()
        playwright = MagicMock()

        async def close_context():
            events.append("context.close")
            close_started.set()
            await allow_close_finish.wait()
            events.append("context.closed")

        async def stop_playwright():
            events.append("playwright.stop")

        context.close = AsyncMock(side_effect=close_context)
        playwright.stop = AsyncMock(side_effect=stop_playwright)

        async def scenario():
            cleanup = asyncio.create_task(
                patrol._close_session(playwright, None, context)
            )
            await close_started.wait()
            cleanup.cancel()
            await asyncio.sleep(0)
            cleanup.cancel()
            await asyncio.sleep(0)
            allow_close_finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await cleanup

        asyncio.run(scenario())

        # Cancellation of one closer must not skip closing later resources.
        self.assertEqual(
            ["context.close", "context.closed", "playwright.stop"], events
        )
        playwright.stop.assert_awaited_once()

    def test_timeout_during_persistent_startup_closes_handles_before_releasing_lock(self):
        account = base_account()
        path = isolate_account_data(self, [account])
        events = []
        context = MagicMock()
        playwright = MagicMock()

        async def close_context():
            events.append("context.close")

        async def stop_playwright():
            events.append("playwright.stop")

        context.close = AsyncMock(side_effect=close_context)
        playwright.stop = AsyncMock(side_effect=stop_playwright)

        async def slow_start(*args, **kwargs):
            events.append("startup.begin")
            # The patrol timeout fires while Playwright is still launching.
            await asyncio.sleep(0.05)
            events.append("startup.return")
            return playwright, context, "fake-profile"

        def release(*args):
            events.append("lock.release")
            browser_module.release_browser_account_lock(*args)

        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(browser_module, "BROWSER_ACCOUNT_LOCK_DIR", directory))
            stack.enter_context(patch.object(patrol, "get_persistent_browser_context", side_effect=slow_start))
            stack.enter_context(patch.object(patrol, "release_browser_account_lock", side_effect=release))
            stack.enter_context(patch.object(patrol, "douyin_network_modes", return_value=["direct"]))
            stack.enter_context(patch.object(patrol, "account_timeout_seconds", return_value=0.01))
            result = asyncio.run(patrol.patrol_account(
                account,
                now=NOW,
                profile_config={
                    "enabled": True, "root": directory,
                    "syncStoredCookiesBeforeRun": False, "seedCookiesWhenEmpty": False,
                },
            ))
            self.assertEqual([], list(Path(directory).glob("*.lock")))
        self.assertEqual(
            ["startup.begin", "startup.return", "context.close", "playwright.stop", "lock.release"],
            events,
        )
        context.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()
        self.assertEqual("timeout", result["login"]["category"])
        self.assertEqual("incomplete", stored(path)["patrol"]["conclusion"])

    def test_startup_cookie_failure_also_closes_handles(self):
        context = MagicMock()
        playwright = MagicMock()
        context.add_cookies = AsyncMock(side_effect=RuntimeError("cookie setup failed"))
        context.close = AsyncMock()
        playwright.stop = AsyncMock()
        with patch.object(
            patrol, "get_persistent_browser_context",
            new=AsyncMock(return_value=(playwright, context, "fake-profile")),
        ):
            with self.assertRaisesRegex(RuntimeError, "cookie setup failed"):
                asyncio.run(patrol._open_session(base_account(), "direct", {
                    "enabled": True, "root": "fake-profile",
                    "syncStoredCookiesBeforeRun": True, "seedCookiesWhenEmpty": False,
                }))
        context.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    def test_manual_patrol_push_runs_in_a_thread(self):
        isolate_account_data(self, [base_account()])
        harness = PatrolHarness(self, login_prompt=True)
        with patch.object(patrol.asyncio, "to_thread", wraps=asyncio.to_thread) as threaded:
            result = harness.run(trigger="manual")
        threaded.assert_awaited_once()
        self.assertIs(notifications.notify_patrol_finished, threaded.call_args.args[0])
        self.assertEqual("login_failed", result["accounts"][0]["conclusion"])
        harness.sent.assert_called_once()

    def test_manual_thread_push_failure_does_not_change_patrol_result(self):
        isolate_account_data(self, [base_account()])
        harness = PatrolHarness(self, login_prompt=True)
        with patch.object(patrol.asyncio, "to_thread", new=AsyncMock(side_effect=RuntimeError("push failed"))):
            result = harness.run(trigger="manual")
        self.assertEqual("login_failed", result["accounts"][0]["conclusion"])
        self.assertIsNone(result["notified"])


if __name__ == "__main__":
    unittest.main()
