"""Offline tests for the explicit legacy retry-timezone backfill."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from zoneinfo import ZoneInfo
import unittest


UTC = timezone.utc
CUTOFF = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
KEY = "refusal_retry_reservations"


def account():
    return {
        "account_ref": "synthetic-account",
        "unique_id": "synthetic-physical-account",
        "login_generation": "synthetic-generation",
        KEY: {
            "synthetic-target": {
                "day": "2026-10-31",
                "at": "2026-10-30T16:30:00+00:00",
                "runId": "synthetic-run",
                "identityRefs": ["synthetic-target", "peer:synthetic-peer"],
            }
        },
    }


@contextmanager
def in_runtime_cwd(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class RetryQuotaMigrationTests(unittest.TestCase):
    def test_plan_backfills_only_legacy_timezone_with_generation_snapshot(self):
        from core.retry_quota_migration import plan_backfill

        source = [account()]
        plan = plan_backfill(
            source,
            account_refs=["synthetic-account"],
            legacy_timezone="Asia/Shanghai",
            writer_cutoff=CUTOFF,
            now=NOW,
        )

        self.assertEqual(source, [account()])
        self.assertEqual({"synthetic-account": "synthetic-generation"}, plan.generations)
        self.assertEqual([("synthetic-account", "synthetic-target")], plan.changes)
        self.assertEqual(
            "Asia/Shanghai",
            plan.rows[0][KEY]["synthetic-target"]["timezone"],
        )
        expected = deepcopy(source)
        expected[0][KEY]["synthetic-target"]["timezone"] = "Asia/Shanghai"
        self.assertEqual(expected, plan.rows)

    def test_backfilled_timezone_uses_calendar_day_while_unknown_stays_conservative(self):
        from core import refusal_retry
        from core.retry_quota_migration import plan_backfill

        original = account()
        plan = plan_backfill(
            [original],
            account_refs=["synthetic-account"],
            legacy_timezone="Asia/Shanghai",
            writer_cutoff=CUTOFF,
            now=NOW,
        )
        migrated = plan.rows[0][KEY]["synthetic-target"]
        unknown = original[KEY]["synthetic-target"]
        zone = ZoneInfo("Asia/Shanghai")
        just_before_midnight = datetime(2026, 10, 31, 23, 59, tzinfo=zone)
        just_after_midnight = datetime(2026, 11, 1, 0, 1, tzinfo=zone)

        self.assertTrue(refusal_retry._reservation_used_on_day(migrated, just_before_midnight, zone))
        self.assertFalse(refusal_retry._reservation_used_on_day(migrated, just_after_midnight, zone))
        self.assertTrue(refusal_retry._reservation_used_on_day(unknown, just_after_midnight, zone))

    def test_dry_run_never_writes_or_creates_a_backup(self):
        from scripts.migrate_retry_timezone import migrate

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            target = data_dir / "usersData.json"
            target.write_text(json.dumps([account()]), encoding="utf-8")
            before = target.read_bytes()

            result = migrate(
                data_dir,
                account_refs=["synthetic-account"],
                legacy_timezone="Asia/Shanghai",
                writer_cutoff=CUTOFF,
                now=NOW,
                apply=False,
            )

            self.assertEqual("dry-run", result["mode"])
            self.assertEqual(1, result["planned_updates"])
            self.assertEqual(before, target.read_bytes())
            self.assertFalse((data_dir / ".data-backups").exists())

    def test_apply_uses_canonical_write_and_private_backup(self):
        from scripts.migrate_retry_timezone import migrate

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            target = data_dir / "usersData.json"
            target.write_text(json.dumps([account()]), encoding="utf-8")
            before = target.read_bytes()

            with in_runtime_cwd(data_dir):
                result = migrate(
                    data_dir,
                    account_refs=["synthetic-account"],
                    legacy_timezone="Asia/Shanghai",
                    writer_cutoff=CUTOFF,
                    now=NOW,
                    apply=True,
                )

            self.assertEqual("applied", result["mode"])
            self.assertEqual(1, result["updated_reservations"])
            backup = Path(result["backup"])
            self.assertEqual(before, backup.read_bytes())
            self.assertEqual(0o600, backup.stat().st_mode & 0o777)
            stored = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual("Asia/Shanghai", stored[0][KEY]["synthetic-target"]["timezone"])

    def test_cas_conflict_preserves_new_credentials(self):
        from scripts import migrate_retry_timezone as migration
        from utils.config import update_user_data as canonical_update

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            target = data_dir / "usersData.json"
            target.write_text(json.dumps([account()]), encoding="utf-8")

            def drift_then_update(mutator, **kwargs):
                newer = account()
                newer["cookies"] = [{"name": "session", "value": "new-credential"}]
                target.write_text(json.dumps([newer]), encoding="utf-8")
                return canonical_update(mutator, **kwargs)

            with patch.object(migration, "update_user_data", side_effect=drift_then_update):
                with self.assertRaisesRegex(ValueError, "changed after migration snapshot"):
                    with in_runtime_cwd(data_dir):
                        migration.migrate(
                            data_dir,
                            account_refs=["synthetic-account"],
                            legacy_timezone="Asia/Shanghai",
                            writer_cutoff=CUTOFF,
                            now=NOW,
                            apply=True,
                        )
            stored = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual("new-credential", stored[0]["cookies"][0]["value"])
            self.assertNotIn("timezone", stored[0][KEY]["synthetic-target"])


if __name__ == "__main__":
    unittest.main()
