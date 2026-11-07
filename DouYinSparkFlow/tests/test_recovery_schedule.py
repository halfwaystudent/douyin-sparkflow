import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import ops


RECOVERY_CONFIG = {
    "automaticRecovery": {"enabled": True},
    "dailySendWindow": {
        "enabled": True,
        "startHour": 10,
        "endHour": 17,
        "scheduleIntervalMinutes": 20,
    },
}


class RecoveryScheduleTests(unittest.TestCase):
    def test_recovery_window_uses_single_10_to_23_pass_and_local_reconcile_at_configured_end(self):
        with (
            patch.object(ops, "running_in_container", return_value=True),
            patch.object(ops, "get_config", return_value=RECOVERY_CONFIG),
        ):
            crontab = ops.replace_douyin_cron_schedule(
                "5 2 * * * /opt/unrelated-backup.sh\n",
                "10:00-17:00/20m",
            )

        lines = crontab.splitlines()
        recovery = [line for line in lines if "SPARKFLOW_RECOVERY_PASS=1" in line]
        reconcile = [line for line in lines if "SPARKFLOW_RECONCILE_ONLY=1" in line]
        closing = [line for line in reconcile if "SPARKFLOW_RECOVERY_CLOSE=1" in line]
        end_reconcile = [line for line in reconcile if "SPARKFLOW_RECOVERY_CLOSE=1" not in line]
        patrol = [line for line in lines if ops.is_patrol_schedule_line(line)]

        self.assertEqual(1, len(recovery))
        self.assertTrue(recovery[0].startswith("*/20 10-23 * * * "))
        self.assertEqual(2, len(reconcile))
        self.assertEqual(1, len(closing))
        self.assertTrue(closing[0].startswith("50 23 * * * "))
        self.assertEqual(1, len(end_reconcile))
        self.assertTrue(end_reconcile[0].startswith("0 17 * * * "))
        self.assertNotIn("SPARKFLOW_FALLBACK_PHASE=1", crontab)
        self.assertEqual(1, len(patrol))
        self.assertTrue(patrol[0].startswith("30 9 * * * "))
        self.assertIn("5 2 * * * /opt/unrelated-backup.sh", crontab)

    def test_recovery_lines_are_classified_and_aligned_with_the_configured_window(self):
        with (
            patch.object(ops, "running_in_container", return_value=True),
            patch.object(ops, "get_config", return_value=RECOVERY_CONFIG),
        ):
            crontab = ops.replace_douyin_cron_schedule("", "10:00-17:00/20m")
            with patch.object(ops, "read_crontab", return_value=crontab):
                alignment = ops.get_schedule_alignment(recent_triggers=[])

        self.assertEqual(["reconcile", "recovery"], alignment["kinds"])
        self.assertTrue(alignment["aligned"], alignment["detail"])
        self.assertEqual("10:00-17:00/20m", alignment["configLabel"])
        self.assertEqual("09:30", alignment["patrolExpected"])

    def test_day_end_window_uses_only_the_2350_close_reconciliation(self):
        day_end = {
            "automaticRecovery": {"enabled": True},
            "dailySendWindow": {
                "enabled": True,
                "startHour": 10,
                "endHour": 24,
                "scheduleIntervalMinutes": 20,
            },
        }
        with (
            patch.object(ops, "running_in_container", return_value=True),
            patch.object(ops, "get_config", return_value=day_end),
        ):
            crontab = ops.replace_douyin_cron_schedule("", "10:00-24:00/20m")

        reconcile = [line for line in crontab.splitlines() if "SPARKFLOW_RECONCILE_ONLY=1" in line]
        self.assertEqual(1, len(reconcile))
        self.assertTrue(reconcile[0].startswith("50 23 * * * "))
        self.assertIn("SPARKFLOW_RECOVERY_CLOSE=1", reconcile[0])
        self.assertNotIn("0 24 * * *", crontab)

    def test_recovery_patrol_stays_at_0930_for_a_custom_business_window(self):
        custom = {
            "automaticRecovery": {"enabled": True},
            "dailySendWindow": {
                "enabled": True,
                "startHour": 12,
                "endHour": 19,
                "scheduleIntervalMinutes": 20,
            },
        }
        with (
            patch.object(ops, "running_in_container", return_value=True),
            patch.object(ops, "get_config", return_value=custom),
        ):
            crontab = ops.replace_douyin_cron_schedule("", "12:00-19:00/20m")

        patrol = [line for line in crontab.splitlines() if ops.is_patrol_schedule_line(line)]
        self.assertEqual(1, len(patrol))
        self.assertTrue(patrol[0].startswith("30 9 * * * "))


    def test_disabled_recovery_window_does_not_recreate_legacy_sends_or_display_recovery_as_fixed(self):
        disabled = {
            "automaticRecovery": {"enabled": True},
            "dailySendWindow": {"enabled": False},
        }
        stale = (
            "*/20 10-23 * * * env SPARKFLOW_RECOVERY_PASS=1 bash /app/scripts/run_scheduled_task.sh\n"
            "0 17 * * * env SPARKFLOW_RECONCILE_ONLY=1 bash /app/scripts/run_scheduled_task.sh\n"
            "30 9 * * * env SPARKFLOW_TASK_MODE=patrol bash /app/scripts/run_scheduled_task.sh\n"
            "5 2 * * * /opt/unrelated-backup.sh\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cron_path = Path(temp_dir) / "root"
            cron_path.write_text(stale, encoding="utf-8")
            with (
                patch.object(ops, "HOST_CRONTAB_PATH", cron_path),
                patch.object(ops, "running_in_container", return_value=True),
                patch.object(ops, "get_config", return_value=disabled),
                patch.object(ops, "read_crontab", side_effect=lambda: cron_path.read_text(encoding="utf-8")),
            ):
                self.assertEqual("", ops.current_daily_schedule())
                result = ops.sync_daily_schedule_from_config()

            text = cron_path.read_text(encoding="utf-8")

        self.assertEqual(0, result.returncode)
        self.assertNotIn("run_scheduled_task.sh", text)
        self.assertIn("5 2 * * * /opt/unrelated-backup.sh", text)

    def test_sync_regenerates_custom_window_idempotently_without_legacy_fallbacks(self):
        custom = {
            "automaticRecovery": {"enabled": True},
            "dailySendWindow": {
                "enabled": True,
                "startHour": 12,
                "endHour": 19,
                "scheduleIntervalMinutes": 20,
            },
        }
        old = (
            "*/20 10-16 * * * bash /app/scripts/run_scheduled_task.sh\n"
            "0 17 * * * env SPARKFLOW_FALLBACK_PHASE=1 bash /app/scripts/run_scheduled_task.sh\n"
            "0 20 * * * env SPARKFLOW_8610_RETRY_ONLY=1 bash /app/scripts/run_scheduled_task.sh\n"
            "5 2 * * * /opt/unrelated-backup.sh\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cron_path = Path(temp_dir) / "root"
            cron_path.write_text(old, encoding="utf-8")
            with (
                patch.object(ops, "HOST_CRONTAB_PATH", cron_path),
                patch.object(ops, "running_in_container", return_value=True),
                patch.object(ops, "get_config", return_value=custom),
                patch.object(ops, "read_crontab", side_effect=lambda: cron_path.read_text(encoding="utf-8")),
            ):
                first = ops.sync_daily_schedule_from_config()
                once = cron_path.read_text(encoding="utf-8")
                second = ops.sync_daily_schedule_from_config()
                twice = cron_path.read_text(encoding="utf-8")

        self.assertEqual(0, first.returncode)
        self.assertEqual(0, second.returncode)
        self.assertEqual(once, twice)
        self.assertEqual(1, once.count("SPARKFLOW_RECOVERY_PASS=1"))
        self.assertIn("0 19 * * *", once)
        self.assertIn("SPARKFLOW_RECONCILE_ONLY=1", once)
        self.assertNotIn("SPARKFLOW_FALLBACK_PHASE=1", once)
        self.assertNotIn("SPARKFLOW_8610_RETRY_ONLY=1", once)
        self.assertIn("5 2 * * * /opt/unrelated-backup.sh", once)

    def test_legacy_window_schedule_is_unchanged_when_recovery_is_not_explicitly_enabled(self):
        legacy = {
            "dailySendWindow": {
                "enabled": True,
                "startHour": 10,
                "endHour": 17,
                "scheduleIntervalMinutes": 20,
            },
        }
        with (
            patch.object(ops, "running_in_container", return_value=True),
            patch.object(ops, "get_config", return_value=legacy),
        ):
            crontab = ops.replace_douyin_cron_schedule("", "10:00-17:00/20m")

        self.assertIn("*/20 10-16 * * *", crontab)
        self.assertIn("SPARKFLOW_FALLBACK_PHASE=1", crontab)
        self.assertNotIn("SPARKFLOW_RECOVERY_PASS=1", crontab)
        self.assertNotIn("SPARKFLOW_RECONCILE_ONLY=1", crontab)

    def test_recovery_flag_does_not_change_fixed_time_mode(self):
        with (
            patch.object(ops, "running_in_container", return_value=True),
            patch.object(ops, "get_config", return_value=RECOVERY_CONFIG),
        ):
            crontab = ops.replace_douyin_cron_schedule("", "18:20")

        self.assertIn("20 18 * * *", crontab)
        self.assertNotIn("SPARKFLOW_RECOVERY_PASS=1", crontab)
        self.assertNotIn("SPARKFLOW_RECONCILE_ONLY=1", crontab)


if __name__ == "__main__":
    unittest.main()
