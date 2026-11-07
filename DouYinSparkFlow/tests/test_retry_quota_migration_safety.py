"""Rejection and real-lock/storage checks using synthetic, temporary data."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout
from core import browser, refusal_retry, tasks
from core.retry_quota_migration import plan_backfill
from scripts import migrate_retry_timezone as migration
from tests.test_retry_quota_migration import account, CUTOFF, NOW, KEY, in_runtime_cwd


def plan(rows, **overrides):
    kwargs = dict(account_refs=['synthetic-account'], legacy_timezone='Asia/Shanghai',
                  writer_cutoff=CUTOFF, now=NOW)
    kwargs.update(overrides)
    return plan_backfill(rows, **kwargs)


class RetryQuotaMigrationSafetyTests(unittest.TestCase):
    def test_invalid_legacy_metadata_stops_without_mutating_input(self):
        mutations = [
            {'day': '2026-10-30'}, {'day': 'not-a-date'}, {'at': 'not-a-time'},
            {'at': '2026-10-30T16:30:00'}, {'at': '2026-11-03T16:30:00+00:00'},
            {'day': '2026-11-01', 'at': CUTOFF.isoformat()},
            {'runId': ''}, {'runId': 1}, {'identityRefs': 'not-a-list'},
            {'identityRefs': []}, {'identityRefs': ['same', 'same']},
        ]
        for changes in mutations:
            with self.subTest(fields=tuple(changes)):
                rows = [account()]
                rows[0][KEY]['synthetic-target'].update(changes)
                before = deepcopy(rows)
                with self.assertRaises(ValueError):
                    plan(rows)
                self.assertEqual(before, rows)

    def test_missing_duplicate_and_stale_accounts_are_rejected(self):
        cases = [([account()], {'account_refs': ['missing']}),
                 ([account(), account()], {}),
                 ([account()], {'account_refs': ['synthetic-account'] * 2}),
                 ([account()], {'account_refs': []}),
                 ([account()], {'expected_generations': {'synthetic-account': 'newer'}})]
        for rows, overrides in cases:
            with self.subTest(overrides=overrides):
                before = deepcopy(rows)
                with self.assertRaises(ValueError):
                    plan(rows, **overrides)
                self.assertEqual(before, rows)

    def test_unproven_timezone_cutoff_or_generation_is_rejected(self):
        for overrides in [{'legacy_timezone': 'Unknown/Nowhere'},
                          {'writer_cutoff': datetime(2026, 11, 1)},
                          {'writer_cutoff': datetime(2026, 11, 3, tzinfo=timezone.utc)},
                          {'now': datetime(2026, 11, 2)}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                plan([account()], **overrides)
        rows = [account()]
        rows[0]['login_generation'] = ''
        with self.assertRaises(ValueError):
            plan(rows)

    def test_existing_timezone_and_unknown_send_barrier_are_unchanged(self):
        rows = [account(), {'account_ref': 'untouched', 'cookies': ['synthetic']}]
        rows[0]['target_states'] = {'synthetic-target': {'status': 'sent_unverified', 'runId': 'keep'}}
        untouched = deepcopy(rows)
        first = plan(rows)
        self.assertEqual(untouched, rows)
        self.assertEqual(rows[0]['target_states'], first.rows[0]['target_states'])
        second = plan(first.rows)
        self.assertEqual([], second.changes)
        self.assertEqual(first.rows, second.rows)
        rows[0][KEY]['synthetic-target']['timezone'] = 'Asia/Tokyo'
        existing = plan(rows)
        self.assertEqual([], existing.changes)
        self.assertEqual(rows, existing.rows)

    def test_timezone_edits_do_not_reopen_either_protected_day(self):
        rows = [account()]
        rows[0][KEY]['synthetic-target']['at'] = '2026-10-31T09:05:00+00:00'
        value = plan(rows).rows[0][KEY]['synthetic-target']
        for now, zone in [
            (datetime(2026, 10, 31, 15, 30, tzinfo=timezone.utc), 'Asia/Tokyo'),
            (datetime(2026, 10, 31, 16, 1, tzinfo=timezone.utc), 'America/Los_Angeles'),
        ]:
            with self.subTest(zone=zone):
                self.assertTrue(refusal_retry._reservation_used_on_day(value, now, ZoneInfo(zone)))

    def test_new_reservation_keeps_timezone_and_same_day_limit(self):
        user = plan([account()]).rows[0]
        zone = ZoneInfo('Asia/Shanghai')
        ref = refusal_retry.reserve(user, 'synthetic-target', NOW, 'synthetic-new-run', schedule_timezone=zone)
        self.assertIsNotNone(ref)
        self.assertEqual('Asia/Shanghai', user[KEY][ref]['timezone'])
        self.assertTrue(refusal_retry.used_today(user, 'synthetic-target', NOW, schedule_timezone=zone))
        self.assertIsNone(refusal_retry.reserve(user, 'synthetic-target', NOW, 'synthetic-repeat', schedule_timezone=zone))

    def test_real_global_storage_lock_blocks_migration(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'usersData.json'
            target.write_text(json.dumps([account()]))
            original = target.read_bytes()
            with in_runtime_cwd(root), FileLock(str(target) + '.lock'):
                with self.assertRaises(Timeout):
                    migration.migrate(root, account_refs=['synthetic-account'], legacy_timezone='Asia/Shanghai',
                                      writer_cutoff=CUTOFF, now=NOW, apply=True)
            self.assertEqual(original, target.read_bytes())
            self.assertFalse((Path(root) / '.data-backups').exists())

    def test_real_task_run_lock_blocks_apply(self):
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root)
            target = data_dir / 'usersData.json'
            target.write_text(json.dumps([account()]))
            original = target.read_bytes()
            with in_runtime_cwd(data_dir), tasks.task_run_lock():
                with self.assertRaises(tasks.TaskRunAlreadyInProgress):
                    migration.migrate(data_dir, account_refs=['synthetic-account'],
                                      legacy_timezone='Asia/Shanghai', writer_cutoff=CUTOFF,
                                      now=NOW, apply=True)
            self.assertEqual(original, target.read_bytes())
            self.assertFalse((data_dir / '.data-backups').exists())

    def test_real_browser_account_lock_blocks_apply(self):
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root)
            target = data_dir / 'usersData.json'
            target.write_text(json.dumps([account()]))
            original = target.read_bytes()
            with in_runtime_cwd(data_dir):
                handle, lock_path, token = asyncio.run(
                    browser.acquire_browser_account_lock(
                        account(), 'synthetic-account', wait_seconds=0,
                    )
                )
                try:
                    with self.assertRaises(Timeout):
                        migration.migrate(data_dir, account_refs=['synthetic-account'],
                                          legacy_timezone='Asia/Shanghai', writer_cutoff=CUTOFF,
                                          now=NOW, apply=True)
                finally:
                    browser.release_browser_account_lock(
                        handle, lock_path, token, 'synthetic-account',
                    )
            self.assertEqual(original, target.read_bytes())
            self.assertFalse((data_dir / '.data-backups').exists())

    def test_migration_account_guard_still_blocks_apply(self):
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root)
            target = data_dir / 'usersData.json'
            target.write_text(json.dumps([account()]))
            original = target.read_bytes()
            guard = migration._account_locks(data_dir, [account()], ['synthetic-account'])[0]
            with in_runtime_cwd(data_dir), FileLock(str(guard)):
                with self.assertRaises(Timeout):
                    migration.migrate(data_dir, account_refs=['synthetic-account'],
                                      legacy_timezone='Asia/Shanghai', writer_cutoff=CUTOFF,
                                      now=NOW, apply=True)
            self.assertEqual(original, target.read_bytes())
            self.assertFalse((data_dir / '.data-backups').exists())

    def test_apply_rejects_data_dir_outside_runtime_cwd_before_backup(self):
        with tempfile.TemporaryDirectory() as data_root, tempfile.TemporaryDirectory() as runtime_root:
            data_dir = Path(data_root)
            target = data_dir / 'usersData.json'
            target.write_text(json.dumps([account()]))
            original = target.read_bytes()
            guard = migration._account_locks(data_dir, [account()], ['synthetic-account'])[0]
            with in_runtime_cwd(runtime_root), FileLock(str(guard)):
                with self.assertRaisesRegex(ValueError, 'data_dir must equal current runtime CWD'):
                    migration.migrate(data_dir, account_refs=['synthetic-account'],
                                      legacy_timezone='Asia/Shanghai', writer_cutoff=CUTOFF,
                                      now=NOW, apply=True)
            self.assertEqual(original, target.read_bytes())
            self.assertFalse((data_dir / '.data-backups').exists())

    def test_encrypted_data_and_backup_stay_encrypted(self):
        from cryptography.fernet import Fernet
        from utils import secure_storage
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root)
            key = data_dir / 'storage.key'
            key.write_bytes(Fernet.generate_key())
            key.chmod(0o600)
            target = data_dir / 'usersData.json'
            with patch.dict(os.environ, {'SPARKFLOW_STORAGE_KEY_FILE': str(key),
                                         'SPARKFLOW_STORAGE_REQUIRE_ENCRYPTED': '1'}):
                target.write_text(secure_storage.serialize_json([account()], target))
                original = target.read_bytes()
                with in_runtime_cwd(data_dir):
                    result = migration.migrate(root, account_refs=['synthetic-account'],
                                               legacy_timezone='Asia/Shanghai',
                                               writer_cutoff=CUTOFF, now=NOW, apply=True)
                self.assertTrue(secure_storage.encrypted_payload(json.loads(target.read_text())))
                self.assertEqual(original, Path(result['backup']).read_bytes())
                self.assertEqual('Asia/Shanghai', secure_storage.read_json(target)[0][KEY]['synthetic-target']['timezone'])


if __name__ == '__main__':
    unittest.main()
