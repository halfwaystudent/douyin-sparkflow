"""An opt-in host-owned JSON snapshot avoids Docker access in the web container."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webui import ops
from tests import test_shared_security as fixtures


class ReadOnlyHostStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'status.json'
        self.environment = patch.dict(os.environ, {'SPARKFLOW_HOST_STATUS_FILE': str(self.path)})
        self.environment.start(); self.addCleanup(self.environment.stop)
        cache = patch.object(ops, '_host_status_cache', None)
        cache.start(); self.addCleanup(cache.stop)

    def write_snapshot(self, *, age=0, rows=None):
        value = {'version': 1, 'checkedAt': (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
                 'containers': rows if rows is not None else [
                     {'Names': 'douyin-web', 'State': 'running', 'Status': '运行中', 'internal-secret': 'must-not-leak'},
                     {'Names': 'login-desktop', 'State': 'running', 'Status': '运行中'},
                     {'Names': 'douyin-scheduler', 'State': 'not_deployed', 'Status': '未部署（未启用定时发送）'},
                     {'Names': 'mihomo', 'State': 'not_deployed', 'Status': '未部署'}]}
        self.path.write_text(json.dumps(value))

    def test_snapshot_is_read_without_any_docker_access_and_only_exports_known_fields(self):
        self.write_snapshot()
        with patch.object(ops, 'get_container_status', side_effect=AssertionError('Docker must not be used')):
            result = ops.get_host_status_snapshot()
        self.assertTrue(result['ok'])
        self.assertEqual(4, len(result['containers']))
        self.assertEqual('not_deployed', result['containers'][2]['State'])
        self.assertNotIn('internal-secret', json.dumps(result))

    def test_expired_snapshot_does_not_claim_services_are_running_or_fall_back_to_docker(self):
        self.write_snapshot(age=90)
        with patch.object(ops, 'get_container_status', side_effect=AssertionError('No Docker fallback')):
            result = ops.get_host_status_snapshot()
        self.assertFalse(result['ok'])
        self.assertEqual([], result['containers'])
        self.assertIn('过期', result['error'])

    def test_broken_snapshots_return_a_clear_failure_without_host_paths(self):
        bad = [None, '{', '[]', json.dumps({'version': 1}), 'x' * 40000]
        for content in bad:
            with self.subTest(content=str(content)[:30]):
                if content is not None:
                    self.path.write_text(content)
                else:
                    self.path.unlink(missing_ok=True)
                with patch.object(ops, '_host_status_cache', None), patch.object(ops, 'get_container_status', side_effect=AssertionError('No Docker fallback')):
                    result = ops.get_host_status_snapshot()
                self.assertFalse(result['ok'])
                self.assertEqual([], result['containers'])
                self.assertNotIn(str(self.path), json.dumps(result))

    def test_future_or_timezone_free_timestamps_are_not_treated_as_fresh(self):
        for stamp in [(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), datetime.now().isoformat()]:
            self.write_snapshot()
            snapshot = json.loads(self.path.read_text()); snapshot['checkedAt'] = stamp
            self.path.write_text(json.dumps(snapshot))
            with patch.object(ops, '_host_status_cache', None):
                self.assertFalse(ops.get_host_status_snapshot()['ok'])

    def test_snapshot_refresh_is_not_hidden_by_the_legacy_five_second_cache(self):
        self.write_snapshot()
        self.assertTrue(ops.get_host_status_snapshot()['ok'])
        self.write_snapshot(age=90)
        self.assertFalse(ops.get_host_status_snapshot()['ok'])

    def test_unrelated_services_are_never_returned(self):
        self.write_snapshot(rows=[{'Names': 'other-project', 'State': 'running', 'Status': 'secret'}])
        result = ops.get_host_status_snapshot()
        self.assertFalse(result['ok'])
        self.assertNotIn('other-project', json.dumps(result))

    def test_unknown_duplicate_or_invalid_rows_reject_the_whole_snapshot(self):
        for field, value in [('Names', 'other-project'), ('Names', 'login-desktop'), ('State', []), ('Status', ''), ('Status', 'x' * 161)]:
            with self.subTest(field=field, value=str(value)[:30]):
                self.write_snapshot()
                snapshot = json.loads(self.path.read_text()); snapshot['containers'][0][field] = value
                self.path.write_text(json.dumps(snapshot))
                self.assertFalse(ops.get_host_status_snapshot()['ok'])

    def test_snapshot_cannot_be_read_by_normal_or_signed_out_users(self):
        fixture = fixtures.SharedConsoleTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.write_snapshot()
        self.assertEqual(403, fixture.login().get('/api/ops/host-status').status_code)
        self.assertEqual(401, fixture.client.get('/api/ops/host-status').status_code)
        response = fixture.login('owner', 'Owner-secret-2026!').get('/api/ops/host-status')
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()['ok'])
