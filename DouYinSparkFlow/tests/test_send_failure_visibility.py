"""Send-failure visibility: A1-A4 of send-failure-visibility-and-patrol.

- A1 a weak send (sent_unverified) from an earlier day does not hide today's failure.
- A2 a weak send from the same day still blocks the failure and is not resent.
- A3 a refused failure write is logged as a state-machine rejection, not as a
  missing account.
- A4 run reports, Web daily stats and the daily summary only present enabled
  accounts and today's results ("not_sent_today" for older states).
"""
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from core import notifications, streak_state, tasks
from utils import notify
from webui import ops as web_ops


TZ = ZoneInfo("Asia/Shanghai")
YESTERDAY = datetime(2026, 10, 13, 11, 0, tzinfo=TZ)
TODAY = datetime(2026, 10, 14, 11, 0, tzinfo=TZ)
TODAY_LATER = datetime(2026, 10, 14, 15, 0, tzinfo=TZ)
WINDOW_ON = tasks._normalize_send_window(
    {"dailySendWindow": {"enabled": True, "startHour": 10, "endHour": 18, "scheduleIntervalMinutes": 20}}
)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _account(name="主号", targets=("抹茶叔叔",), enabled=True, unique_id="1001"):
    return {
        "account_ref": f"ref-{unique_id}",
        "unique_id": unique_id,
        "username": name,
        "enabled": enabled,
        "targets": list(targets),
    }


class FakeStore:
    """In-memory stand-in for utils.config.update_user_data."""

    def __init__(self, accounts):
        self.accounts = accounts
        self.saves = 0

    def update(self, mutator, **_kwargs):
        result = mutator(self.accounts)
        changed = True
        if isinstance(result, tuple) and len(result) == 2:
            result, changed = result
        if changed:
            self.saves += 1
        return deepcopy(result)


def _fixed_datetime(value):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value if tz is None else value.astimezone(tz)

    return FixedDatetime


class YesterdayWeakSendDoesNotHideTodayFailureTests(unittest.TestCase):
    """A1."""

    def test_state_machine_records_today_failure_over_yesterday_sent_unverified(self):
        account = _account()
        streak_state.mark_sent_unverified(account, "抹茶叔叔", run_id="r1", now=YESTERDAY)

        state = streak_state.mark_failed(
            account,
            "抹茶叔叔",
            category="friend_not_found",
            reason="target not selected from friend list",
            retryable=True,
            now=TODAY,
        )

        self.assertEqual(streak_state.STATE_FAILED_RETRYABLE, state["status"])
        self.assertEqual("friend_not_found", state["lastErrorCategory"])
        self.assertEqual(TODAY.isoformat(timespec="seconds"), state["lastAttemptAt"])
        stored = streak_state.target_state(account, "抹茶叔叔", TODAY)
        self.assertEqual(streak_state.STATE_FAILED_RETRYABLE, stored["status"])

    def test_browser_failure_after_yesterday_sent_unverified_writes_state_and_failure_queue(self):
        stored = _account()
        streak_state.mark_sent_unverified(stored, "抹茶叔叔", run_id="r1", now=YESTERDAY)
        user = deepcopy(stored)
        store = FakeStore([stored])

        with patch.object(tasks, "update_user_data", store.update):
            tasks._persist_browser_send_failure(
                user,
                "抹茶叔叔",
                "",
                "friend_not_found",
                "target not selected from friend list",
                _iso(TODAY),
            )

        for account in (stored, user):
            state = streak_state.target_state(account, "抹茶叔叔", TODAY)
            self.assertEqual(streak_state.STATE_FAILED_RETRYABLE, state["status"])
            self.assertEqual("friend_not_found", state["lastErrorCategory"])
            entry = account["failure_queue"]["抹茶叔叔"]
            self.assertEqual("friend_not_found", entry["category"])
            self.assertEqual(_iso(TODAY), entry["lastAttemptAt"])

    def test_yesterday_needs_verification_clears_after_in_flight_failure_today(self):
        account = _account()
        original = streak_state.mark_sent_unverified(
            account, "抹茶叔叔", run_id="r1", now=YESTERDAY
        )
        self.assertTrue(original["needsVerification"])
        _, claimed = streak_state.claim_in_flight(
            account,
            "抹茶叔叔",
            run_id="r2",
            strategy="browser",
            now=TODAY,
            allow_retry=True,
        )
        self.assertTrue(claimed)
        state = streak_state.mark_failed(
            account,
            "抹茶叔叔",
            category="friend_not_found",
            reason="target not selected",
            retryable=True,
            now=TODAY,
        )
        self.assertEqual(streak_state.STATE_FAILED_RETRYABLE, state["status"])
        self.assertFalse(state["needsVerification"])

    def test_same_day_needs_verification_is_preserved_by_in_flight_failure(self):
        account = _account()
        streak_state.mark_sent_unverified(account, "抹茶叔叔", run_id="r1", now=TODAY)
        # A same-day weak send blocks a claim, so model a legitimate in-flight
        # retry with a carried verification flag and same-day sentAt explicitly.
        state = streak_state.target_state(account, "抹茶叔叔", TODAY)
        state.update(
            status=streak_state.STATE_IN_FLIGHT,
            startedAt=TODAY_LATER.isoformat(timespec="seconds"),
            sentAt=TODAY.isoformat(timespec="seconds"),
            needsVerification=True,
        )
        account["target_states"][state["targetRef"]] = state
        failed = streak_state.mark_failed(
            account,
            "抹茶叔叔",
            category="browser_timeout",
            reason="retry failed",
            retryable=True,
            now=TODAY_LATER,
        )
        self.assertTrue(failed["needsVerification"])

    def test_run_report_redacts_peer_and_sec_target_refs(self):
        from core import friend_identity

        account = _account(targets=("peer target",))
        friend_identity.bind_target(
            account, "peer target", "98765432101234567", source="web", now=TODAY
        )
        streak_state.mark_send_confirmed(account, "peer target", now=TODAY)
        records = _report_records([account], completed_at=TODAY_LATER)
        target_ref = records[0]["targetRef"]
        self.assertTrue(target_ref.startswith("peer:h-"))
        self.assertNotIn("98765432101234567", target_ref)

        sec_account = _account(targets=("sec target",))
        friend_identity.bind_target(
            sec_account,
            "sec target",
            "98765432101234567",
            "MS4wLjABAAAA-full-secuid",
            source="web",
            now=TODAY,
        )
        streak_state.mark_send_confirmed(sec_account, "sec target", now=TODAY)
        sec_records = _report_records([sec_account], completed_at=TODAY_LATER)
        self.assertTrue(sec_records[0]["targetRef"].startswith("sec:h-"))
        self.assertNotIn("MS4wLjABAAAA-full-secuid", str(sec_records))
        self.assertNotIn("98765432101234567", str(sec_records))

    def test_run_report_hashes_unique_id_fallback_account_ref(self):
        account = _account(targets=("peer target",), unique_id="98765432101234567")
        account.pop("account_ref")
        account.pop("username")
        account["account_ref"] = account["unique_id"]
        friend_identity = __import__("core.friend_identity", fromlist=["bind_target"])
        friend_identity.bind_target(
            account, "peer target", "98765432101234567", source="web", now=TODAY
        )
        streak_state.mark_send_confirmed(account, "peer target", now=TODAY)
        records = _report_records([account], completed_at=TODAY_LATER)
        self.assertTrue(records[0]["accountRef"].startswith("account:h-"))
        self.assertNotIn("98765432101234567", str(records))

    def test_run_report_lists_today_failure_with_category_after_yesterday_sent_unverified(self):
        account = _account()
        streak_state.mark_sent_unverified(account, "抹茶叔叔", run_id="r1", now=YESTERDAY)
        streak_state.mark_failed(
            account,
            "抹茶叔叔",
            category="friend_not_found",
            reason="target not selected from friend list",
            retryable=True,
            now=TODAY,
        )

        records = _report_records([account], completed_at=TODAY_LATER)

        self.assertEqual(1, len(records))
        self.assertEqual(streak_state.STATE_FAILED_RETRYABLE, records[0]["status"])
        self.assertEqual("friend_not_found", records[0]["category"])
        self.assertEqual("target not selected from friend list", records[0]["reason"])

    def test_final_point_notifies_today_failure_after_yesterday_sent_unverified(self):
        account = _account()
        streak_state.mark_sent_unverified(account, "抹茶叔叔", run_id="r1", now=YESTERDAY)
        streak_state.mark_failed(
            account,
            "抹茶叔叔",
            category="friend_not_found",
            reason="target not selected from friend list",
            retryable=True,
            now=TODAY,
        )

        messages = _notify([account], now=datetime(2026, 10, 14, 18, 0, tzinfo=TZ), fallback=True)

        failure = [item for item in messages if item["event"] == "failure"]
        summary = [item for item in messages if item["event"] == "daily_summary"]
        self.assertEqual(1, len(failure))
        self.assertIn("目标「抹茶叔叔」：好友列表中找不到该目标", failure[0]["text"])
        self.assertIn("成功目标：0 个", summary[0]["text"])
        self.assertIn("好友列表中找不到该目标", summary[0]["text"])

    def test_mid_day_retryable_failure_after_yesterday_weak_send_waits_for_final_point(self):
        account = _account()
        streak_state.mark_sent_unverified(account, "抹茶叔叔", run_id="r1", now=YESTERDAY)
        state = streak_state.mark_failed(
            account,
            "抹茶叔叔",
            category="friend_not_found",
            reason="target not selected from friend list",
            retryable=True,
            now=TODAY,
        )

        # Yesterday's verification flag must not ride along onto today's failure.
        self.assertFalse(state.get("needsVerification"))
        messages = _notify([account], now=datetime(2026, 10, 14, 12, 0, tzinfo=TZ), fallback=False)
        self.assertEqual([], [item for item in messages if item["event"] == "failure"])


class SameDayWeakSendStillBlocksFailureTests(unittest.TestCase):
    """A2."""

    def test_same_day_sent_unverified_keeps_status_when_failure_arrives(self):
        account = _account()
        streak_state.mark_sent_unverified(account, "抹茶叔叔", run_id="r1", now=TODAY)

        state = streak_state.mark_failed(
            account,
            "抹茶叔叔",
            category="friend_not_found",
            reason="later run could not find the target",
            retryable=True,
            now=TODAY_LATER,
        )

        self.assertEqual(streak_state.STATE_SENT_UNVERIFIED, state["status"])
        self.assertTrue(streak_state.is_sent_unverified(account, "抹茶叔叔", TODAY_LATER))

    def test_prefer_state_keeps_same_day_sent_unverified_over_failed_candidate(self):
        existing = {"status": streak_state.STATE_SENT_UNVERIFIED, "sentAt": _iso(TODAY)}
        candidate = {
            "status": streak_state.STATE_FAILED_RETRYABLE,
            "sentAt": _iso(TODAY),
            "lastAttemptAt": _iso(TODAY_LATER),
            "lastErrorCategory": "friend_not_found",
        }
        self.assertFalse(streak_state._prefer_state(candidate, existing))

    def test_same_day_sent_unverified_failure_is_not_queued_and_not_resent(self):
        stored = _account()
        streak_state.mark_sent_unverified(stored, "抹茶叔叔", run_id="r1", now=TODAY)
        user = deepcopy(stored)
        store = FakeStore([stored])

        with patch.object(tasks, "update_user_data", store.update):
            tasks._persist_browser_send_failure(
                user,
                "抹茶叔叔",
                "",
                "friend_not_found",
                "later run could not find the target",
                _iso(TODAY_LATER),
            )

        self.assertNotIn("failure_queue", stored)
        self.assertEqual(0, store.saves)
        state = streak_state.target_state(stored, "抹茶叔叔", TODAY_LATER)
        self.assertEqual(streak_state.STATE_SENT_UNVERIFIED, state["status"])
        _, claimed = streak_state.claim_in_flight(
            stored,
            "抹茶叔叔",
            run_id="r2",
            strategy="browser",
            now=TODAY_LATER + timedelta(minutes=20),
        )
        self.assertFalse(claimed)


class RejectedFailureWriteLoggingTests(unittest.TestCase):
    """A3."""

    def test_rejected_failure_write_logs_state_machine_rejection_with_existing_state(self):
        stored = _account()
        streak_state.mark_sent_unverified(stored, "抹茶叔叔", run_id="r1", now=TODAY)
        user = deepcopy(stored)
        store = FakeStore([stored])

        with (
            patch.object(tasks, "update_user_data", store.update),
            self.assertLogs(tasks.logger, level="WARNING") as captured,
        ):
            tasks._persist_browser_send_failure(
                user,
                "抹茶叔叔",
                "",
                "friend_not_found",
                "later run could not find the target",
                _iso(TODAY_LATER),
            )

        text = "\n".join(captured.output)
        self.assertNotIn("Could not find account", text)
        self.assertIn("State machine rejected browser send failure", text)
        self.assertIn("existingStatus=sent_unverified", text)
        self.assertIn(f"existingEventAt={TODAY.isoformat(timespec='seconds')}", text)
        self.assertIn("rejection=sent_unverified_today", text)
        self.assertIn("requested=friend_not_found", text)

    def test_missing_account_still_logs_could_not_find_account(self):
        store = FakeStore([_account(name="其他账号", unique_id="2002")])

        with (
            patch.object(tasks, "update_user_data", store.update),
            self.assertLogs(tasks.logger, level="WARNING") as captured,
        ):
            tasks._persist_browser_send_failure(
                _account(),
                "抹茶叔叔",
                "",
                "friend_not_found",
                "target not selected from friend list",
                _iso(TODAY),
            )

        text = "\n".join(captured.output)
        self.assertIn("Could not find account to persist browser send failure", text)
        self.assertNotIn("State machine rejected", text)

    def test_rejected_success_write_is_not_logged_as_missing_account(self):
        stored = _account()
        streak_state.mark_send_confirmed(stored, "抹茶叔叔", now=TODAY_LATER)
        user = deepcopy(stored)
        store = FakeStore([stored])

        with (
            patch.object(tasks, "update_user_data", store.update),
            self.assertLogs(tasks.logger, level="WARNING") as captured,
        ):
            tasks._persist_browser_send_success(user, "抹茶叔叔", "hi", _iso(TODAY))

        text = "\n".join(captured.output)
        self.assertNotIn("Could not find account", text)
        self.assertIn("State machine rejected browser send success", text)
        self.assertIn("existingStatus=send_confirmed", text)


def _report_records(users, *, completed_at):
    with (
        patch.object(tasks, "datetime", _fixed_datetime(completed_at.astimezone(timezone.utc))),
        patch.object(streak_state, "append_run_report") as append,
        patch.object(streak_state, "update_weekly_summary") as weekly,
        patch.object(streak_state, "prune_run_reports"),
    ):
        tasks._append_streak_run_report(
            "run-today",
            completed_at.astimezone(timezone.utc) - timedelta(seconds=5),
            {"proxyAddress": ""},
            users,
            run_status="completed",
        )
    records = [call.args[0] for call in append.call_args_list]
    weekly_records = weekly.call_args.args[0]
    assert records == weekly_records
    return records


def _notify(users, *, now, fallback=False, manual=None):
    sent = []
    config = {"notifications": {"enabled": True}}
    with tempfile.TemporaryDirectory() as temp_dir:
        with (
            patch.object(notify, "enabled_channel_names", return_value=["webhook"]),
            patch.object(notify, "send_message", side_effect=lambda message, _cfg: sent.append(message)),
        ):
            notifications.notify_run_finished(
                "run-1",
                "completed",
                config,
                users,
                schedule_tz=TZ,
                send_window=WINDOW_ON,
                manual_run=fallback if manual is None else manual,
                fallback_run=fallback,
                now=now.astimezone(timezone.utc),
                state_path=Path(temp_dir) / "notify_state.json",
            )
    return sent


class TodayOnlyReportAndStatsTests(unittest.TestCase):
    """A4."""

    def setUp(self):
        self.disabled = _account(name="半路大学生", targets=("srx666",), enabled=False, unique_id="3003")
        streak_state.mark_failed(
            self.disabled,
            "srx666",
            category="send_rejected",
            reason="8101",
            retryable=False,
            now=datetime(2026, 10, 9, 11, 0, tzinfo=TZ),
        )
        self.disabled["failure_queue"] = {
            "srx666": {
                "category": "send_rejected",
                "reason": "8101",
                "lastAttemptAt": _iso(TODAY),
                "attemptCount": 1,
            }
        }
        self.enabled = _account(targets=("昨天失败", "昨天弱确认", "今天成功"))
        streak_state.mark_failed(
            self.enabled,
            "昨天失败",
            category="send_rejected",
            reason="old refusal",
            retryable=False,
            now=YESTERDAY,
        )
        self.enabled["failure_queue"] = {
            "昨天失败": {
                "category": "send_rejected",
                "reason": "old refusal",
                "lastAttemptAt": _iso(YESTERDAY),
                "attemptCount": 1,
            }
        }
        streak_state.mark_sent_unverified(self.enabled, "昨天弱确认", run_id="r0", now=YESTERDAY)
        self.enabled["message_history"] = {
            "昨天弱确认": {
                "sentAt": _iso(YESTERDAY),
                "confirmationLevel": "weak",
                "confirmationSource": "browser_visible_count_increased",
                "needsVerification": True,
            }
        }
        streak_state.mark_send_confirmed(self.enabled, "今天成功", now=TODAY)

    def test_run_report_skips_disabled_accounts(self):
        records = _report_records([self.disabled, self.enabled], completed_at=TODAY_LATER)

        self.assertEqual(3, len(records))
        self.assertNotIn("ref-3003", {record["accountRef"] for record in records})
        self.assertNotIn("srx666", {record["displayName"] for record in records})

    def test_run_report_marks_older_states_not_sent_today_without_old_reason(self):
        records = {
            record["displayName"]: record
            for record in _report_records([self.enabled], completed_at=TODAY_LATER)
        }

        for name in ("昨天失败", "昨天弱确认"):
            record = records[name]
            self.assertEqual(tasks.REPORT_STATUS_NOT_SENT_TODAY, record["status"])
            self.assertEqual("not_sent_today", record["status"])
            self.assertEqual("", record["category"])
            self.assertEqual("", record["reason"])
            self.assertEqual("", record["sentAt"])
            self.assertEqual("", record["confirmationSource"])
            self.assertFalse(record["needsVerification"])
            self.assertTrue(record["targetRef"])
        self.assertEqual(streak_state.STATE_SEND_CONFIRMED, records["今天成功"]["status"])

    def test_run_report_with_only_disabled_accounts_writes_run_level_record(self):
        records = _report_records([self.disabled], completed_at=TODAY_LATER)

        self.assertEqual(1, len(records))
        self.assertEqual("", records[0]["accountRef"])
        self.assertEqual("completed", records[0]["status"])

    def test_web_today_stats_exclude_disabled_accounts_and_older_failures(self):
        with (
            patch.object(web_ops, "get_userData", return_value=[deepcopy(self.disabled), deepcopy(self.enabled)]),
            patch.object(web_ops, "_normalize_send_window", return_value={"enabled": False}),
            patch.object(web_ops, "datetime", _fixed_datetime(TODAY_LATER)),
        ):
            snapshot = web_ops.get_send_console_snapshot()

        summary = snapshot["summary"]
        self.assertEqual(1, summary["enabled_accounts"])
        self.assertEqual(3, summary["total_targets"])
        self.assertEqual(0, summary["today_failed_targets"])
        self.assertEqual(1, summary["today_confirmed_targets"])
        self.assertEqual(["主号"], [row["username"] for row in snapshot["accounts"]])

    def test_daily_summary_excludes_disabled_accounts_and_older_states(self):
        messages = _notify(
            [self.disabled, self.enabled],
            now=datetime(2026, 10, 14, 18, 0, tzinfo=TZ),
            fallback=True,
        )

        summary = [item for item in messages if item["event"] == "daily_summary"][0]["text"]
        self.assertIn("启用目标：3 个", summary)
        self.assertIn("成功目标：1 个", summary)
        self.assertNotIn("半路大学生", summary)
        self.assertNotIn("消息被平台拒绝", summary)
        failure_text = "\n".join(item["text"] for item in messages if item["event"] == "failure")
        self.assertNotIn("srx666", failure_text)
        self.assertIn("目标「昨天失败」：当天未发送成功", failure_text)


if __name__ == "__main__":
    unittest.main()
